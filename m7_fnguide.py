# -*- coding: utf-8 -*-
"""
m7_fnguide.py — FnGuide 컨센서스 주간 스냅샷 수집기
====================================================
목적: 컨센서스(영업이익 추정치·목표주가)는 과거 소급이 불가능하므로
      매주 스냅샷을 찍어 리비전 시계열을 직접 축적한다.
부수입: WICS 업종 분류가 같이 수집되어 산업→종목 매핑테이블이 된다.

유니버스: KRX 시가총액 상위 N (기본 300, 환경변수 M7_TOP_N로 조절)
저장:
  data/m7_revision/YYYY-MM-DD.csv   (당일 스냅샷)
  data/m7_revision/history.csv      (누적, run_date+code+metric+period 기준 중복제거)
스키마(long): run_date, code, name, wics, metric, period, value
  metric = op_e(영업이익 추정, 억원) / rev_e(매출액 추정, 억원) / target_price(원)
실행: GitHub Actions 주간 cron 또는 로컬에서 `python m7_fnguide.py`
"""
import io
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
TOP_N = int(os.environ.get("M7_TOP_N", "300"))
SLEEP = float(os.environ.get("M7_SLEEP", "0.7"))
OUT_DIR = os.path.join("data", "m7_revision")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Referer": "https://comp.fnguide.com/",
}


# ---------------------------------------------------------------- universe
def get_universe(top_n: int = TOP_N) -> pd.DataFrame:
    """KRX 시총 상위 N 보통주. 실패 시 예외."""
    import FinanceDataReader as fdr

    df = fdr.StockListing("KRX")
    code_col = "Code" if "Code" in df.columns else "Symbol"
    df = df[df[code_col].astype(str).str.len() == 6].copy()
    df[code_col] = df[code_col].astype(str)
    # 보통주만(코드 끝자리 0), 스팩 제외
    df = df[df[code_col].str.endswith("0")]
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
            print(f"[warn] FDR 실패({e}) -> 직전 유니버스 {len(u)}종목 재사용")
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


def _annual_estimates(html: str) -> dict:
    """Financial Highlight 연간 테이블에서 (E) 컬럼의 매출액/영업이익 추출.
    반환: {("op_e","2026/12"): 12345.0, ("rev_e","2027/12"): ...} (단위: 억원)"""
    out = {}
    try:
        tables = pd.read_html(io.StringIO(html), thousands=",")
    except ValueError:
        return out

    for t in tables:
        cols = [str(c[-1]) if isinstance(c, tuple) else str(c) for c in t.columns]
        if not any("(E)" in c for c in cols):
            continue
        # 분기 테이블 제외(연간 = /12 컬럼만 존재)
        if any(re.search(r"/(03|06|09)", c) for c in cols):
            continue
        first = t.iloc[:, 0].astype(str).str.strip()
        if not (first == "영업이익").any():
            continue

        for metric, row_kr in (("op_e", "영업이익"), ("rev_e", "매출액")):
            mask = first == row_kr
            if not mask.any():
                continue
            ridx = mask[mask].index[0]
            for j, c in enumerate(cols):
                if "(E)" not in c:
                    continue
                period = c.replace("(E)", "").strip()
                val = pd.to_numeric(t.iloc[t.index.get_loc(ridx), j],
                                    errors="coerce")
                if pd.notna(val):
                    out[(metric, period)] = float(val)
        if out:
            break  # 첫 번째로 매칭된 연간(연결) 테이블만 사용
    return out


def parse(code: str, name: str, html: str) -> list:
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    wics = _extract_wics(soup)
    rows = []

    tp = _extract_target_price(text)
    if tp is not None:
        rows.append([TODAY, code, name, wics, "target_price", "", tp])

    for (metric, period), val in _annual_estimates(html).items():
        rows.append([TODAY, code, name, wics, metric, period, val])

    return rows


# ---------------------------------------------------------------- main
def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    uni = get_universe_safe()
    print(f"[start] {TODAY} 유니버스 {len(uni)}종목, sleep={SLEEP}s")

    all_rows, fail = [], 0
    for i, row in uni.iterrows():
        code, name = str(row["Code"]), str(row["Name"])
        html = fetch(code)
        if html is None:
            fail += 1
        else:
            try:
                all_rows += parse(code, name, html)
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"[skip] {code} {name}: {e}")
        if (i + 1) % 25 == 0:
            print(f"  ...{i + 1}/{len(uni)} (rows={len(all_rows)}, fail={fail})")
        time.sleep(SLEEP)

    df = pd.DataFrame(
        all_rows,
        columns=["run_date", "code", "name", "wics", "metric", "period", "value"],
    )
    if df.empty:
        print("[error] 수집 0건 — 해외 IP 차단 가능성. PC(국내 IP)에서 로컬 실행 후 "
              "CSV를 repo에 업로드하는 폴백을 사용할 것. (README 참조)")
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

    n_codes = df["code"].nunique()
    n_wics = df.loc[df["wics"] != "", "wics"].nunique()
    print(f"[done] {TODAY}: {n_codes}종목 / {len(df)}행 / 실패 {fail} / WICS {n_wics}개 업종")


if __name__ == "__main__":
    main()
