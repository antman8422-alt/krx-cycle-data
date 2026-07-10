# -*- coding: utf-8 -*-
"""
m7_naver.py 겸 m7_fnguide.py (v5) — 컨센서스 주간 스냅샷 수집기 (네이버 금융판)
==============================================================================
목적: 컨센서스(영업이익 추정치)는 과거 소급이 불가능하므로
      매주 스냅샷을 찍어 리비전 시계열을 직접 축적한다.
부수입: 업종 분류가 같이 수집되어 산업→종목 매핑테이블이 된다.

v5 변경점 (v4 대비) — 데이터 소스 교체:
- FnGuide가 해외 데이터센터 IP에 종목 조회를 거부(기본 페이지로 대체)함을
  확인 → 소스를 네이버 금융 종목 메인(finance.naver.com)으로 교체
- 네이버는 서버 렌더링이라 브라우저 불필요 → requests로 복귀, 실행 5분대
- '기업실적분석' 표에서 연간 (E) 컬럼의 매출액/영업이익 추출
  (연간/분기가 한 표에 공존 → thead 1행의 '연간' colspan으로 연간 열만 선별)
- 목표주가: 투자의견 표에서 구조적으로 추출 (숫자 오인 방지)
- 업종: 동일업종 링크 텍스트 (wics 필드에 저장)

스키마(long): run_date, code, name, wics, metric, period, value
  metric = op_e(영업이익 추정, 억원) / rev_e(매출액 추정, 억원) / target_price(원)
실행: GitHub Actions 주간 cron 또는 로컬 `python m7_fnguide.py`
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
SLEEP = float(os.environ.get("M7_SLEEP", "0.4"))
OUT_DIR = os.path.join("data", "m7_revision")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://finance.naver.com/",
}


def log(msg):
    print(msg, flush=True)


def _url(code: str) -> str:
    return f"https://finance.naver.com/item/main.naver?code={code}"


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
def fetch(code: str):
    """반환: (html or None, status ok/redirected/fail)"""
    for _ in range(2):
        try:
            r = requests.get(_url(code), headers=HEADERS, timeout=15)
            if r.status_code == 200 and len(r.text) > 5000:
                html = r.text
                page = _pagecode(html)
                if page == code:
                    return html, "ok"
                return html, "redirected"
        except requests.RequestException:
            pass
        time.sleep(2)
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


def _pagecode(html: str) -> str:
    """종목명 옆 <span class="code">005930</span>에서 실제 페이지 코드 추출."""
    soup = BeautifulSoup(html[:20000], "lxml")
    node = soup.select_one("span.code")
    if node:
        m = re.search(r"\d{6}", node.get_text())
        if m:
            return m.group(0)
    m = re.search(r'class="code"[^>]*>\s*(\d{6})', html)
    return m.group(1) if m else ""


def _extract_industry(soup: BeautifulSoup) -> str:
    """동일업종 링크(업종 상세) 텍스트 = 업종명."""
    a = soup.select_one("a[href*='upjong']")
    return a.get_text(strip=True) if a else ""


def _extract_target_price(soup: BeautifulSoup):
    """투자의견 표: <th>투자의견 l 목표주가</th> 옆 <td>의 마지막 숫자 em.
    '4.00매수' 같은 의견 점수를 목표가로 오인하지 않도록 구조적으로 추출."""
    for th in soup.find_all("th"):
        if "목표주가" not in th.get_text():
            continue
        td = th.find_next("td")
        if not td:
            continue
        cand = None
        for em in td.find_all("em"):
            v = _clean_num(em.get_text(strip=True))
            if v is not None:
                cand = v          # 마지막 숫자(목표주가)가 남는다
        if cand is not None and cand > 100:   # 의견점수(1~5) 배제 안전핀
            return cand
    m = re.search(r"목표주가[^0-9]{0,30}?([0-9]{2,3}(?:,[0-9]{3})+)",
                  soup.get_text(" ", strip=True))
    if m:
        return _clean_num(m.group(1))
    return None


def _find_perf_table(soup: BeautifulSoup):
    """'기업실적분석' 표를 내용으로 탐지: thead에 (E) 컬럼 + '연간' 라벨,
    tbody에 '영업이익' 행. 반환: (table, 기간리스트, 연간열 인덱스 집합)"""
    for table in soup.find_all("table"):
        thead, tbody = table.find("thead"), table.find("tbody")
        if not (thead and tbody):
            continue
        hrows = thead.find_all("tr")
        row_last = [c.get_text(strip=True)
                    for c in hrows[-1].find_all(["th", "td"])]
        if not any("(E)" in p for p in row_last):
            continue
        labels = []
        for tr in tbody.find_all("tr"):
            cell = tr.find(["th", "td"])
            if cell:
                labels.append(re.sub(r"\s+", "", cell.get_text(" ", strip=True)))
        if "영업이익" not in labels:
            continue

        # 연간 열 선별: thead 1행의 '연간' colspan 구간
        ann_idx = set()
        if len(hrows) >= 2:
            pos = 0
            for c in hrows[0].find_all(["th", "td"]):
                if c.get("rowspan"):      # '주요재무정보' 라벨 칸은 열 미점유
                    continue
                span = int(c.get("colspan", 1))
                if "연간" in c.get_text(strip=True):
                    ann_idx.update(range(pos, pos + span))
                pos += span
        if not ann_idx:                   # 폴백: 12월 결산 기간만 연간 취급
            ann_idx = {i for i, p in enumerate(row_last)
                       if re.search(r"\d{4}[./]12", p)}
        return table, row_last, ann_idx
    return None, None, None


def _annual_estimates(soup: BeautifulSoup) -> dict:
    """연간 (E) 컬럼에서 매출액/영업이익 추출. 단위: 억원.
    반환: {("op_e","2026/12"): 605000.0, ...}"""
    out = {}
    table, periods, ann_idx = _find_perf_table(soup)
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
        pers = periods[1:] if len(periods) == len(vals) + 1 else periods
        offset = 1 if pers is not periods else 0
        for i, p in enumerate(pers):
            if "(E)" not in p or i >= len(vals):
                continue
            if ann_idx and (i + offset) not in ann_idx:
                continue                  # 분기 (E)는 제외
            v = _clean_num(vals[i].get_text(strip=True))
            if v is not None:
                period = p.replace("(E)", "").strip().replace(".", "/")
                out[(metric, period)] = v
    return out


def parse(code: str, name: str, html: str) -> list:
    soup = BeautifulSoup(html, "lxml")
    wics = _extract_industry(soup)
    rows = []

    tp = _extract_target_price(soup)
    if tp is not None:
        rows.append([TODAY, code, name, wics, "target_price", "", tp])

    for (metric, period), val in _annual_estimates(soup).items():
        rows.append([TODAY, code, name, wics, metric, period, val])

    return rows


def _diagnose(req_code: str, html: str):
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else "(no title)"
    page = _pagecode(html)
    mismatch = " (불일치)" if page and page != req_code else ""
    log(f"[diag] 요청코드={req_code} 페이지코드={page or '?'}{mismatch}")
    log(f"[diag] len={len(html)} title='{title[:60]}' "
        f"tables={len(soup.find_all('table'))}")
    log(f"[diag] html 내 '(E)' {html.count('(E)')}회 / "
        f"'영업이익' {html.count('영업이익')}회 / "
        f"'기업실적분석' {html.count('기업실적분석')}회")


# ---------------------------------------------------------------- main
def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    uni = get_universe_safe()
    log(f"[start] {TODAY} 유니버스 {len(uni)}종목, sleep={SLEEP}s (naver)")

    all_rows = []
    n_ok = n_empty = n_fail = n_redirect = 0
    last_html, last_code = None, ""
    diagnosed = False

    for i, row in uni.iterrows():
        code, name = str(row["Code"]), str(row["Name"])
        html, status = fetch(code)
        if html is None:
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
                n_empty += 1
                log(f"[skip] {code} {name}: {type(e).__name__}: {e}")

        if i == 4 and not all_rows and not diagnosed and last_html:
            _diagnose(last_code, last_html)
            diagnosed = True

        if (i + 1) % 25 == 0 or (i + 1) == len(uni):
            log(f"  ...{i + 1}/{len(uni)} (ok={n_ok}, empty={n_empty}, "
                f"redirect={n_redirect}, fail={n_fail})")
        time.sleep(SLEEP)

    df = pd.DataFrame(
        all_rows,
        columns=["run_date", "code", "name", "wics", "metric", "period", "value"],
    )
    if df.empty:
        log("[error] 수집 0건 — 위 [diag] 참조")
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
        f"/ 실패 {n_fail} / 총 {len(df)}행 / 업종 {n_wics}개")


if __name__ == "__main__":
    main()
