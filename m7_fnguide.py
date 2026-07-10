# -*- coding: utf-8 -*-
"""
m7_fnguide.py (v2) — FnGuide 컨센서스 주간 스냅샷 수집기
=========================================================
목적: 컨센서스(영업이익 추정치·목표주가)는 과거 소급이 불가능하므로
      매주 스냅샷을 찍어 리비전 시계열을 직접 축적한다.
부수입: WICS 업종 분류가 같이 수집되어 산업→종목 매핑테이블이 된다.

v2 변경점:
- pandas read_html 의존 제거 → Financial Highlight 표를 BeautifulSoup으로 직접 파싱
  (pandas 3.x에서 read_html이 FnGuide 표 구조에 IndexError를 던지는 문제 해결)
- 진단 모드: 초기 5종목에서 수집 0건이면 페이지 상태([diag])를 자동 출력
- 모든 로그 flush → Actions에서 실시간 확인 가능

유니버스: KRX 시가총액 상위 N (기본 300, 환경변수 M7_TOP_N로 조절)
저장:
  data/m7_revision/YYYY-MM-DD.csv   (당일 스냅샷)
  data/m7_revision/history.csv      (누적, run_date+code+metric+period 중복제거)
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
}


def log(msg):
    print(msg, flush=True)


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
    url = ("https://comp.fnguide.com/SVO2/ASP/SVD_Main.asp"
           f"?pGB=1&gicode=A{code}&cID=&MenuYn=Y&ReportGB=&NewMenuID=Y&stkGb=701")
    for _ in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200 and len(r.text) > 5000:
                return r.text
        except requests.RequestException:
            pass
        time.sleep(2)
    return None


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


def _annual_estimates(soup: BeautifulSoup) -> dict:
    """Financial Highlight 연간 테이블(#highlight_D_A)에서
    (E) 컬럼의 매출액/영업이익을 직접 파싱. 단위: 억원.
    반환: {("op_e","2026/12"): 12345.0, ...}"""
    out = {}
    div = None
    for did in ("highlight_D_A", "highlight_B_A"):   # 연결 우선, 없으면 별도
        div = soup.find(id=did)
        if div:
            break
    if not div:
        return out
    table = div.find("table")
    if not (table and table.find("thead") and table.find("tbody")):
        return out

    hrows = table.find("thead").find_all("tr")
    periods = [c.get_text(strip=True)
               for c in hrows[-1].find_all(["th", "td"])]
    # thead가 한 줄짜리면 첫 칸은 'IFRS(연결)' 같은 라벨이므로 제거
    if len(hrows) == 1 and periods and not re.search(r"\d{4}", periods[0]):
        periods = periods[1:]

    for tr in table.find("tbody").find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        label = re.sub(r"\s+", "", cells[0].get_text(" ", strip=True))
        metric = {"매출액": "rev_e", "영업이익": "op_e"}.get(label)
        if not metric:
            continue
        vals = cells[1:]
        for i, p in enumerate(periods):
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


def _diagnose(code: str, html: str):
    """수집이 전멸일 때 원인 판별용 페이지 상태 출력."""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else "(no title)"
    log(f"[diag] code={code} len={len(html)} title='{title[:60]}' "
        f"tables={len(soup.find_all('table'))} "
        f"highlight_D_A={'O' if soup.find(id='highlight_D_A') else 'X'}")
    log(f"[diag] text[:180]={soup.get_text(' ', strip=True)[:180]}")


# ---------------------------------------------------------------- main
def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    uni = get_universe_safe()
    log(f"[start] {TODAY} 유니버스 {len(uni)}종목, sleep={SLEEP}s")

    all_rows = []
    n_ok = n_empty = n_fetch_fail = n_parse_err = 0
    last_html = None
    diagnosed = False

    for i, row in uni.iterrows():
        code, name = str(row["Code"]), str(row["Name"])
        html = fetch(code)
        if html is None:
            n_fetch_fail += 1
        else:
            last_html = html
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

        # 초기 5종목에서 전멸이면 즉시 원인 진단 1회
        if i == 4 and not all_rows and not diagnosed and last_html:
            _diagnose(code, last_html)
            diagnosed = True

        if (i + 1) % 25 == 0:
            log(f"  ...{i + 1}/{len(uni)} (ok={n_ok}, empty={n_empty}, "
                f"fetch_fail={n_fetch_fail}, parse_err={n_parse_err})")
        time.sleep(SLEEP)

    df = pd.DataFrame(
        all_rows,
        columns=["run_date", "code", "name", "wics", "metric", "period", "value"],
    )
    if df.empty:
        log("[error] 수집 0건 — 위 [diag] 줄로 원인 판별:")
        log("  title이 정상 종목명이 아니거나 tables=0이면 접근 차단 → PC(국내 IP) 로컬 실행 폴백")
        log("  tables는 많은데 highlight_D_A=X면 페이지 구조 변경 → 코드 수정 필요")
        if last_html and not diagnosed:
            _diagnose("(last)", last_html)
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
    log(f"[done] {TODAY}: 성공 {n_ok} / 컨센없음 {n_empty} / 접속실패 {n_fetch_fail} "
        f"/ 파싱에러 {n_parse_err} / 총 {len(df)}행 / WICS {n_wics}개 업종")


if __name__ == "__main__":
    main()
