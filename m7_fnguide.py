# -*- coding: utf-8 -*-
"""
m7_fnguide.py (v3) — FnGuide 컨센서스 주간 스냅샷 수집기
=========================================================
목적: 컨센서스(영업이익 추정치)는 과거 소급이 불가능하므로
      매주 스냅샷을 찍어 리비전 시계열을 직접 축적한다.
부수입: WICS 업종 분류가 같이 수집되어 산업→종목 매핑테이블이 된다.

v3 변경점 (v2 대비):
- 세션 워밍업 + 쿠키 유지: 세션 없는 접근을 기본 페이지(삼성전자)로
  돌려보내는 서버 동작에 대응
- 페이지코드 검증: 응답 페이지의 종목코드가 요청과 다르면 재시도,
  계속 다르면 'redirected'로 집계하고 오염 데이터 저장 방지
- 연간 컨센서스 표를 div id가 아니라 내용으로 탐지
  (thead에 (E) 컬럼 + tbody에 영업이익 행 + 기간 간격 12개월)
- 진단 강화: 실패 시 요청/응답 코드 불일치 여부, 영업이익 표를 품은
  div id 목록, html 내 '(E)' 출현 횟수까지 출력

스키마(long): run_date, code, name, wics, metric, period, value
  metric = op_e(영업이익 추정, 억원) / rev_e(매출액 추정, 억원) / target_price(원)
실행: GitHub Actions 주간 cron 또는 로컬에서 `python m7_fnguide.py`
"""
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime("%Y-%m-%d")
TOP_N = int(os.environ.get("M7_TOP_N", "300") or "300")
SLEEP = float(os.environ.get("M7_SLEEP", "0.7"))
OUT_DIR = os.path.join("data", "m7_revision")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Referer": "https://comp.fnguide.com/",
    "Upgrade-Insecure-Requests": "1",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def log(msg):
    print(msg, flush=True)


def _url(code: str) -> str:
    return ("https://comp.fnguide.com/SVO2/ASP/SVD_Main.asp"
            f"?pGB=1&gicode=A{code}&cID=&MenuYn=Y&ReportGB=&NewMenuID=Y&stkGb=701")


# ---------------------------------------------------------------- universe
def get_universe(top_n: int = TOP_N) -> pd.DataFrame:
    """KRX 시총 상위 N 보통주. 실패 시 예외."""
    import FinanceDataReader as fdr

    df = fdr.StockListing("KRX")
    code_col = "Code" if "Code" in df.columns else "Symbol"
    df = df[df[code_col].astype(str).str.len() == 6].copy()
    df[code_col] = df[code_col].astype(str)
    df = df[df[code_col].str.endswith("0")]          # 보통주만
    df = df[~df["Name"].astype(str).str.contains("스팩")]
    if "Marcap" in df.columns:
        df = df.sort_values("Marcap", ascending=False)
    df = df.head(top_n)
    return df[[code_col, "Name"]].rename(
        columns={code_col: "Code"}).reset_index(drop=True)


def get_universe_safe() -> pd.DataFrame:
    """FDR 실패 시 직전 history의 유니버스 재사용 (수집 연속성 확보)."""
    try:
        return get_universe()
    except Exception as e:  # noqa: BLE001
        hist_path = os.path.join(OUT_DIR, "history.csv")
        if os.path.exists(hist_path):
            h = pd.read_csv(hist_path, dtype={"code": str})
            last = h[h["run_date"] == h["run_date"].max()]
            u = (last[["code", "name"]].drop_duplicates()
                 .rename(columns={"code": "Code", "name": "Name"})
                 .reset_index(drop=True))
            log(f"[warn] FDR 실패({e}) -> 직전 유니버스 {len(u)}종목 재사용")
            return u
        raise


# ---------------------------------------------------------------- fetch
def _pagecode(html: str) -> str:
    """페이지 상단(title 근처)에서 실제 표시 중인 종목코드 추출."""
    m = re.search(r"\((A?\d{6})\)", html[:3000])
    return m.group(1).lstrip("A") if m else ""


def warmup():
    """세션 쿠키 확보용 선행 접속."""
    try:
        SESSION.get(_url("005930"), timeout=15)
        time.sleep(1.0)
    except requests.RequestException:
        pass


def fetch(code: str):
    """반환: (html or None, status)  status ∈ ok / redirected / fail"""
    last_mismatch = None
    for _ in range(3):
        try:
            r = SESSION.get(_url(code), timeout=15)
            if r.status_code == 200 and len(r.text) > 5000:
                if _pagecode(r.text) == code:
                    return r.text, "ok"
                last_mismatch = r.text
                time.sleep(1.5)
                continue
        except requests.RequestException:
            pass
        time.sleep(2)
    if last_mismatch is not None:
        return last_mismatch, "redirected"
    return None, "fail"


# ---------------------------------------------------------------- parse
def _clean_num(s: str):
    s = s.replace(",", "").strip()
    if s in ("", "-", "N/A", "n/a"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _extract_wics(soup: BeautifulSoup) -> str:
    node = soup.find(string=re.compile("WICS"))
    if not node:
        return ""
    return re.sub(r".*WICS\s*:\s*", "", str(node)).strip()


def _extract_target_price(text: str):
    m = re.search(r"목표주가\s*([0-9][0-9,]*)", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _row_labels(tbody) -> list:
    out = []
    for tr in tbody.find_all("tr"):
        cell = tr.find(["th", "td"])
        if cell:
            out.append(re.sub(r"\s+", "", cell.get_text(" ", strip=True)))
    return out


def _find_annual_table(soup: BeautifulSoup):
    """div id에 의존하지 않고 연간 컨센서스 표를 내용으로 탐지.
    조건: thead 마지막 행에 (E) 컬럼 존재 + tbody에 '영업이익' 행 존재.
    복수 후보면 기간 간격이 12개월(연간)인 표를 우선."""
    fallback = None
    for table in soup.find_all("table"):
        thead, tbody = table.find("thead"), table.find("tbody")
        if not (thead and tbody):
            continue
        hrows = thead.find_all("tr")
        periods = [c.get_text(strip=True)
                   for c in hrows[-1].find_all(["th", "td"])]
        if not any("(E)" in p for p in periods):
            continue
        if "영업이익" not in _row_labels(tbody):
            continue
        months = []
        for p in periods:
            m = re.search(r"(\d{4})/(\d{2})", p)
            if m:
                months.append(int(m.group(1)) * 12 + int(m.group(2)))
        gaps = [b - a for a, b in zip(months, months[1:]) if b > a]
        if gaps and min(gaps) >= 12:
            return table, periods          # 연간 표 확정
        if fallback is None:
            fallback = (table, periods)    # 차선(혼합/분기형)
    return fallback if fallback else (None, None)


def _annual_estimates(soup: BeautifulSoup) -> dict:
    """연간 (E) 컬럼에서 매출액/영업이익 추출. 단위: 억원.
    반환: {("op_e","2026/12"): 12345.0, ...}"""
    out = {}
    table, periods = _find_annual_table(soup)
    if table is None:
        return out
    tbody = table.find("tbody")

    for tr in tbody.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        label = re.sub(r"\s+", "", cells[0].get_text(" ", strip=True))
        metric = {"매출액": "rev_e", "영업이익": "op_e"}.get(label)
        if not metric:
            continue
        vals = cells[1:]
        # 헤더-값 정렬: 헤더 행에 라벨 칸이 포함된 구조(1줄 thead)면 한 칸 밀기
        if len(periods) == len(vals) + 1:
            pers = periods[1:]
        else:
            pers = periods
        for i, p in enumerate(pers):
            if "(E)" not in p or i >= len(vals):
                continue
            v = _clean_num(vals[i].get_text(strip=True))
            if v is not None:
                out[(metric, p.replace("(E)", "").strip())] = v
    return out


def parse(code: str, name: str, html: str) -> list:
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    wics = _extract_wics(soup)
    rows = []

    tp = _extract_target_price(text)
    if tp is not None:
        rows.append([TODAY, code, name, wics, "target_price", "", tp])

    for (metric, period), val in _annual_estimates(soup).items():
        rows.append([TODAY, code, name, wics, metric, period, val])

    return rows


def _diagnose(req_code: str, html: str):
    """수집이 전멸일 때 원인 판별용 상태 출력."""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else "(no title)"
    page = _pagecode(html)
    mismatch = " (불일치 -> 세션/봇감지 의심)" if page and page != req_code else ""
    log(f"[diag] 요청코드={req_code} 페이지코드={page or '?'}{mismatch}")
    log(f"[diag] len={len(html)} title='{title[:60]}' "
        f"tables={len(soup.find_all('table'))}")
    ids = sorted({d.get("id") for d in soup.find_all("div", id=True)
                  if d.find("table") and "영업이익" in d.get_text()})
    log(f"[diag] '영업이익' 표를 품은 div id: {ids[:10] if ids else '없음'}")
    log(f"[diag] html 내 '(E)' {html.count('(E)')}회 / "
        f"'영업이익' {html.count('영업이익')}회 "
        f"(둘 다 많은데 수집 0이면 코드 문제, '(E)'가 0이면 값이 JS 지연로딩)")


# ---------------------------------------------------------------- main
def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    uni = get_universe_safe()
    log(f"[start] {TODAY} 유니버스 {len(uni)}종목, sleep={SLEEP}s")
    warmup()

    all_rows = []
    n_ok = n_empty = n_fail = n_redirect = n_parse_err = 0
    last_html, last_code = None, ""
    diagnosed = False

    for i, row in uni.iterrows():
        code, name = str(row["Code"]), str(row["Name"])
        html, status = fetch(code)
        if status == "fail":
            n_fail += 1
        elif status == "redirected":
            n_redirect += 1
            last_html, last_code = html, code
        else:
            last_html, last_code = html, code
            try:
                rows = parse(code, name, html)
                if rows:
                    all_rows += rows
                    n_ok += 1
                else:
                    n_empty += 1
            except Exception as e:  # noqa: BLE001
                n_parse_err += 1
                log(f"[skip] {code} {name}: {type(e).__name__}: {e}")

        if i == 4 and not all_rows and not diagnosed and last_html:
            _diagnose(last_code, last_html)
            diagnosed = True

        if (i + 1) % 25 == 0 or (i + 1) == len(uni):
            log(f"  ...{i + 1}/{len(uni)} (ok={n_ok}, empty={n_empty}, "
                f"redirect={n_redirect}, fail={n_fail}, parse_err={n_parse_err})")
        time.sleep(SLEEP)

    df = pd.DataFrame(
        all_rows,
        columns=["run_date", "code", "name", "wics", "metric", "period", "value"],
    )
    if df.empty:
        log("[error] 수집 0건 — 위 [diag] 줄로 원인 판별 가능")
        if last_html and not diagnosed:
            _diagnose(last_code, last_html)
        sys.exit(1)

    snap_path = os.path.join(OUT_DIR, f"{TODAY}.csv")
    df.to_csv(snap_path, index=False, encoding="utf-8-sig")

    hist_path = os.path.join(OUT_DIR, "history.csv")
    if os.path.exists(hist_path):
        hist = pd.read_csv(hist_path, dtype={"code": str, "period": str})
        merged = pd.concat([hist, df], ignore_index=True)
    else:
        merged = df
    merged = merged.drop_duplicates(
        subset=["run_date", "code", "metric", "period"], keep="last")
    merged.to_csv(hist_path, index=False, encoding="utf-8-sig")

    n_wics = df.loc[df["wics"] != "", "wics"].nunique()
    log(f"[done] {TODAY}: 성공 {n_ok} / 컨센없음 {n_empty} / 리다이렉트 {n_redirect} "
        f"/ 접속실패 {n_fail} / 파싱에러 {n_parse_err} / 총 {len(df)}행 "
        f"/ WICS {n_wics}개 업종")


if __name__ == "__main__":
    main()
