# -*- coding: utf-8 -*-
"""
m7_fnguide.py (v7) — 컨센서스 주간 스냅샷 수집기 (FnGuide 복귀판)
================================================================
v7 변경점 (v6 대비):
- 2026-09-12 네이버 금융이 'Npay 증권'(JS 앱)으로 개편 → 구 item/main.naver
  페이지의 HTML 표가 소멸, 전 종목 redirected 판정으로 수집 0건.
- 소스를 FnGuide(comp.fnguide.com SVD_Main)로 전환. 정적 HTML이라 표 파싱 가능,
  업종(FICS)·목표주가·연간(E) 매출/영업이익 모두 한 페이지에서 나옴.
- 네이버 경로는 M7_SOURCE=naver 로 보존 (환경변수 한 줄로 롤백 가능).
- 파싱 철학은 v6 그대로: 구조 무가정 탐지 + 실패 시 지문 자동 덤프.

스키마(long): run_date, code, name, wics, metric, period, value  (v6과 동일)
실행: GitHub Actions 주간 cron 또는 로컬 `python m7_fnguide.py`
의존성: pip install requests beautifulsoup4 lxml pandas finance-datareader
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
SOURCE = os.environ.get("M7_SOURCE", "fnguide").lower()   # fnguide | naver
OUT_DIR = os.path.join("data", "m7_revision")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}
PERIOD_RE = re.compile(r"\d{4}[./]\d{2}")


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- universe
def get_universe(top_n: int = TOP_N) -> pd.DataFrame:
    import FinanceDataReader as fdr

    df = fdr.StockListing("KRX")
    code_col = "Code" if "Code" in df.columns else "Symbol"
    df = df[df[code_col].astype(str).str.len() == 6].copy()
    df[code_col] = df[code_col].astype(str)
    df = df[df[code_col].str.endswith("0")]
    df = df[~df["Name"].astype(str).str.contains("스팩")]
    if "Marcap" in df.columns:
        df = df.sort_values("Marcap", ascending=False)
    df = df.head(top_n)
    return df[[code_col, "Name"]].rename(
        columns={code_col: "Code"}).reset_index(drop=True)


def get_universe_safe() -> pd.DataFrame:
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
def _url(code: str) -> str:
    if SOURCE == "naver":
        return f"https://finance.naver.com/item/main.naver?code={code}"
    return ("https://comp.fnguide.com/SVO2/ASP/SVD_Main.asp"
            f"?pGB=1&gicode=A{code}&cID=&MenuYn=Y&ReportGB=&NewMenuID=101&stkGb=701")


def _referer() -> str:
    return ("https://finance.naver.com/" if SOURCE == "naver"
            else "https://comp.fnguide.com/")


def fetch(code: str):
    """반환: (html or None, status ok/redirected/fail)"""
    headers = dict(HEADERS, Referer=_referer())
    for _ in range(2):
        try:
            r = requests.get(_url(code), headers=headers, timeout=20)
            if r.status_code == 200 and len(r.text) > 5000:
                html = r.text
                page = _pagecode(html)
                if page == code:
                    return html, "ok"
                # FnGuide는 코드 스팬이 없을 수 있음 — 종목코드 문자열 존재로 2차 판정
                if SOURCE != "naver" and (f"A{code}" in html or code in html):
                    return html, "ok"
                return html, "redirected"
        except requests.RequestException:
            pass
        time.sleep(2)
    return None, "fail"


# ---------------------------------------------------------------- parse (공통)
def _clean_num(s: str):
    s = s.replace(",", "").strip()
    if s in ("", "-", "N/A", "n/a"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _cell_texts(tr) -> list:
    return [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]


def _pagecode(html: str) -> str:
    soup = BeautifulSoup(html[:20000], "lxml")
    node = soup.select_one("span.code")
    if node:
        m = re.search(r"\d{6}", node.get_text())
        if m:
            return m.group(0)
    m = re.search(r'class="code"[^>]*>\s*(\d{6})', html)
    return m.group(1) if m else ""


def _extract_industry(soup: BeautifulSoup) -> str:
    if SOURCE == "naver":
        a = soup.select_one("a[href*='upjong']")
        return a.get_text(strip=True) if a else ""
    # FnGuide: 헤더의 'FICS 반도체 및 관련장비' 류 스팬
    for span in soup.select("span.stxt, p.stxt_group span"):
        t = span.get_text(" ", strip=True)
        if t.startswith("FICS"):
            return t.replace("FICS", "").strip()
    m = re.search(r"FICS\s*[:\s]\s*([가-힣A-Za-z0-9 ,&·/]+)",
                  soup.get_text(" ", strip=True))
    return m.group(1).strip() if m else ""


def _extract_target_price(soup: BeautifulSoup):
    for th in soup.find_all(["th", "dt"]):
        if "목표주가" not in th.get_text():
            continue
        td = th.find_next(["td", "dd"])
        if not td:
            continue
        cand = None
        for em in (td.find_all("em") or [td]):
            v = _clean_num(em.get_text(strip=True))
            if v is not None:
                cand = v
        if cand is not None and cand > 100:
            return cand
    m = re.search(r"목표주가[^0-9]{0,30}?([0-9]{2,3}(?:,[0-9]{3})+)",
                  soup.get_text(" ", strip=True))
    if m:
        return _clean_num(m.group(1))
    return None


def _find_perf(soup: BeautifulSoup):
    """(E) 포함 기간 헤더를 가진 재무 표를 구조 무가정으로 탐지 — v6 로직 유지.
    FnGuide 재무하이라이트: 헤더에 'Annual'/'연간' colspan 행이 있고
    기간은 '2026/12(E)' 형식이라 그대로 걸린다."""
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        if len(trs) < 2:
            continue
        p_idx, periods = None, None
        for i, tr in enumerate(trs[:4]):
            texts = _cell_texts(tr)
            n_period = sum(1 for t in texts if PERIOD_RE.search(t))
            if n_period >= 3 and any("(E)" in t for t in texts):
                p_idx, periods = i, texts
                break
        if p_idx is None:
            continue
        rows = {}
        for tr in trs[p_idx + 1:]:
            cells = tr.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            label = re.sub(r"\s+", "", cells[0].get_text(" ", strip=True))
            # FnGuide 라벨은 '매출액', '영업이익' 뒤에 발생주의 수식이 붙을 수 있음
            base = None
            if label.startswith("매출액"):
                base = "매출액"
            elif label.startswith("영업이익") and "률" not in label and "율" not in label:
                base = "영업이익"
            if base and base not in rows:
                rows[base] = cells[1:]
        if "영업이익" not in rows:
            continue
        while periods and not PERIOD_RE.search(periods[0]):
            periods = periods[1:]
        ann_idx = set()
        for tr in trs[:p_idx]:
            pos, found = 0, False
            for c in tr.find_all(["th", "td"]):
                if c.get("rowspan") and not c.get("colspan"):
                    continue
                span = int(c.get("colspan", 1))
                head = c.get_text(strip=True)
                if "연간" in head or "Annual" in head:
                    ann_idx.update(range(pos, pos + span))
                    found = True
                pos += span
            if found:
                break
        if not ann_idx:
            ann_idx = set(range(min(4, len(periods))))
        return periods, ann_idx, rows
    return None, None, None


def _annual_estimates(soup: BeautifulSoup) -> dict:
    out = {}
    periods, ann_idx, rows = _find_perf(soup)
    if not rows:
        return out
    for label, metric in (("매출액", "rev_e"), ("영업이익", "op_e")):
        vals = rows.get(label)
        if not vals:
            continue
        for i, p in enumerate(periods):
            if "(E)" not in p or i not in ann_idx or i >= len(vals):
                continue
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


# ---------------------------------------------------------------- diag
def _diagnose(req_code: str, html: str):
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else "(no title)"
    page = _pagecode(html)
    mismatch = " (불일치)" if page and page != req_code else ""
    log(f"[diag] source={SOURCE} 요청코드={req_code} 페이지코드={page or '?'}{mismatch}")
    log(f"[diag] len={len(html)} title='{title[:60]}' "
        f"tables={len(soup.find_all('table'))}")


def _diagnose_perf(html: str):
    soup = BeautifulSoup(html, "lxml")
    log("[perf-diag] '영업이익' 포함 테이블 지문:")
    n = 0
    for ti, table in enumerate(soup.find_all("table")):
        txt = table.get_text(" ", strip=True)
        if "영업이익" not in txt:
            continue
        n += 1
        trs = table.find_all("tr")
        r0 = " | ".join(_cell_texts(trs[0]))[:150] if trs else ""
        r1 = " | ".join(_cell_texts(trs[1]))[:150] if len(trs) > 1 else ""
        log(f"  T{ti}: rows={len(trs)} thead={'O' if table.find('thead') else 'X'} "
            f"(E)={'O' if '(E)' in txt else 'X'}")
        log(f"    r0='{r0}'")
        log(f"    r1='{r1}'")
        if n >= 4:
            break
    if n == 0:
        log("  (없음 — '영업이익' 텍스트 자체가 페이지에 없음)")


# ---------------------------------------------------------------- main
def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    uni = get_universe_safe()
    log(f"[start] {TODAY} 유니버스 {len(uni)}종목, sleep={SLEEP}s ({SOURCE})")

    all_rows = []
    n_ok = n_empty = n_fail = n_redirect = n_op = 0
    last_html, last_code = None, ""
    last_ok_html = None
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
            last_ok_html = html
            try:
                rows = parse(code, name, html)
                if rows:
                    all_rows += rows
                    n_ok += 1
                    if any(r[4] == "op_e" for r in rows):
                        n_op += 1
                else:
                    n_empty += 1
            except Exception as e:  # noqa: BLE001
                n_empty += 1
                log(f"[skip] {code} {name}: {type(e).__name__}: {e}")

        if i == 4 and not all_rows and not diagnosed and last_html:
            _diagnose(last_code, last_html)
            diagnosed = True

        if (i + 1) % 25 == 0 or (i + 1) == len(uni):
            log(f"  ...{i + 1}/{len(uni)} (ok={n_ok}, op={n_op}, "
                f"empty={n_empty}, redirect={n_redirect}, fail={n_fail})")
        time.sleep(SLEEP)

    if n_op == 0 and last_ok_html:
        log("[warn] 영업이익E 수집 0건 — 표 구조 지문:")
        _diagnose_perf(last_ok_html)

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
    log(f"[done] {TODAY}: 성공 {n_ok} (op_e {n_op}) / 컨센없음 {n_empty} "
        f"/ 리다이렉트 {n_redirect} / 실패 {n_fail} / 총 {len(df)}행 "
        f"/ 업종 {n_wics}개 / source={SOURCE}")


if __name__ == "__main__":
    main()
