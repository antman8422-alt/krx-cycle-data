# -*- coding: utf-8 -*-
"""
m7_fnguide.py (v8) — 컨센서스 주간 스냅샷 수집기 (네이버 JSON API판 v8.1)
================================================================
연혁:
- v6까지: finance.naver.com HTML 표 파싱 → 2026-09 'Npay 증권' 개편으로 사망
- v7: comp.fnguide.com 복귀 시도 → FnGuide도 wcomp 신버전 개편, 구페이지 스텁화
- v8: 웹페이지 긁기 전면 폐기. 네이버 증권 앱이 쓰는 공개 JSON API로 전환.
      probe.py 정찰(2026-09-14)로 확인된 열린 문:
        · /api/stock/{code}/basic          → 업종
        · /api/stock/{code}/finance/annual → 연간 재무 (isConsensus 필드 내장!)
        · /api/stock/{code}/integration    → 목표주가 탐색
      HTML 시대의 "(E) 문자열 찾기"가 API의 isConsensus 플래그로 대체되어
      구조적으로 더 튼튼해짐. 파싱 실패 시 JSON 지문 자동 덤프(철학 유지).

스키마(long): run_date, code, name, wics, metric, period, value  (기존 동일)
실행: GitHub Actions 주간 cron 또는 로컬 `python m7_fnguide.py`
의존성: pip install requests pandas finance-datareader
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime("%Y-%m-%d")
TOP_N = int(os.environ.get("M7_TOP_N", "300") or "300")
SLEEP = float(os.environ.get("M7_SLEEP", "0.3"))
OUT_DIR = os.path.join("data", "m7_revision")
BASE = "https://m.stock.naver.com/api/stock"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://m.stock.naver.com/",
}
PERIOD_KEY_RE = re.compile(r"^(\d{4})[./]?(\d{2})\.?$")


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
_FAIL_DIAG = {"n": 0}


def fetch_json(url: str):
    """반환: dict/list 또는 None"""
    last_reason = "?"
    for _ in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            last_reason = f"HTTP {r.status_code} len={len(r.text)}"
            if r.status_code == 200 and r.text.strip()[:1] in "[{":
                return r.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            last_reason = f"{type(e).__name__}: {str(e)[:100]}"
        time.sleep(1.5)
    if _FAIL_DIAG["n"] < 3:
        _FAIL_DIAG["n"] += 1
        log(f"[fail-diag] {url.split('/api/')[-1]}: {last_reason}")
    return None


# ---------------------------------------------------------------- json helpers
def _walk(obj, depth=0):
    """JSON 트리의 (key, value) 재귀 순회."""
    if depth > 8:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:50]:
            yield from _walk(v, depth + 1)


def _clean_num(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).replace(",", "").strip()
    if s in ("", "-", "N/A", "n/a", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


_IND_CACHE = {}
_IND_DIAG = {"done": False}


def _industry_code(integ) -> str:
    """integration JSON의 industryCode."""
    if isinstance(integ, dict):
        v = integ.get("industryCode")
        if v:
            return str(v)
        for k, v in _walk(integ):
            if k == "industryCode" and v:
                return str(v)
    return ""


def _industry_name(icode: str) -> str:
    """업종코드 → 업종명 (stocks/industry endpoint, 업종당 1회 캐시).
    이름 필드 미발견 시 지문 덤프 후 코드 문자열로 폴백(리그 묶임은 유지)."""
    if not icode:
        return ""
    if icode in _IND_CACHE:
        return _IND_CACHE[icode]
    js = fetch_json(
        f"https://m.stock.naver.com/api/stocks/industry/{icode}?page=1&pageSize=1")
    name = ""
    if js:
        for k, v in _walk(js):
            if not isinstance(v, str) or not v.strip() or v.strip().isdigit():
                continue
            kl = k.lower()
            if kl.startswith("stock") or kl in ("namekor", "nameeng", "nationname"):
                continue
            if ("industry" in kl or "upjong" in kl
                    or kl in ("groupname", "categoryname", "sectorname")):
                name = v.strip()
                break
    if not name and isinstance(js, dict):
        gi = js.get("groupInfo")
        if isinstance(gi, dict):
            for v in gi.values():
                if (isinstance(v, str) and v.strip()
                        and not v.strip().isdigit()
                        and re.search(r"[가-힣]", v)):
                    name = v.strip()
                    break
    if not name and js and not _IND_DIAG["done"]:
        _IND_DIAG["done"] = True
        log(f"[warn] 업종명 필드 미발견 (code={icode}) — 지문:")
        _fingerprint(js, f"industry/{icode}")
    _IND_CACHE[icode] = name or icode
    return _IND_CACHE[icode]


def _find_target_price(js):
    """integration JSON에서 목표주가 탐색 (key에 target+price 포함)."""
    if not js:
        return None
    for k, v in _walk(js):
        kl = k.lower()
        if "target" in kl and "price" in kl:
            n = _clean_num(v)
            if n is not None and n > 100:
                return n
    return None


def _norm_period(key: str, title: str):
    """'202612' 또는 '2026.12.' → '2026/12'"""
    for cand in (str(key or ""), str(title or "")):
        c = cand.replace("(E)", "").strip()
        m = re.match(r"^(\d{4})[./]?(\d{2})", c)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    return None


def _annual_estimates(js) -> dict:
    """finance/annual JSON → {("op_e","2026/12"): 값, ...}
    구조 무가정: trTitleList(컬럼 정의, isConsensus 플래그)와
    행 리스트(매출액/영업이익 + 컬럼값)를 트리에서 탐색."""
    out = {}
    if not js:
        return out
    # 1) 컬럼 정의: isConsensus 필드를 가진 dict들의 리스트
    cols = []          # [(colkey, period)] — 컨센서스 열만
    for k, v in _walk(js):
        if isinstance(v, list) and v and isinstance(v[0], dict) \
                and "isConsensus" in v[0]:
            for c in v:
                if str(c.get("isConsensus", "N")).upper() != "Y":
                    continue
                period = _norm_period(c.get("key"), c.get("title"))
                colkey = str(c.get("key") or c.get("title") or "")
                if period and colkey:
                    cols.append((colkey, period))
            break
    if not cols:
        return out
    # 2) 행: title/acctName이 매출액/영업이익으로 시작하는 dict
    label_keys = ("title", "acctName", "accountName", "name")
    for k, v in _walk(js):
        if not (isinstance(v, list) and v and isinstance(v[0], dict)):
            continue
        for row in v:
            label = ""
            for lk in label_keys:
                if isinstance(row.get(lk), str):
                    label = re.sub(r"\s+", "", row[lk])
                    break
            metric = None
            if label.startswith("매출액"):
                metric = "rev_e"
            elif label.startswith("영업이익") and "률" not in label and "율" not in label:
                metric = "op_e"
            if not metric:
                continue
            # 값 컨테이너: dict(콜키→값) 또는 columns 하위 dict
            containers = [row]
            for ck in ("columns", "datas", "values", "columnValue"):
                if isinstance(row.get(ck), dict):
                    containers.append(row[ck])
            for colkey, period in cols:
                if (metric, period) in out:
                    continue
                for cont in containers:
                    cell = cont.get(colkey)
                    if isinstance(cell, dict):
                        cell = cell.get("value", cell.get("v"))
                    n = _clean_num(cell)
                    if n is not None:
                        out[(metric, period)] = n
                        break
    return out


# ---------------------------------------------------------------- diag
def _fingerprint(js, label):
    log(f"[json-diag] {label} 최상위 키: "
        f"{list(js.keys())[:12] if isinstance(js, dict) else type(js).__name__}")
    for k, v in _walk(js):
        if isinstance(v, list) and v and isinstance(v[0], dict):
            log(f"  '{k}': list[{len(v)}] 첫 항목 키={list(v[0].keys())[:10]}")


# ---------------------------------------------------------------- main
def collect(code: str, name: str):
    """한 종목 수집. 반환 (rows, ok_flag)"""
    fin = fetch_json(f"{BASE}/{code}/finance/annual")
    integ = fetch_json(f"{BASE}/{code}/integration")
    if fin is None and integ is None:
        return [], False
    wics = _industry_name(_industry_code(integ))
    rows = []
    tp = _find_target_price(integ)
    if tp is not None:
        rows.append([TODAY, code, name, wics, "target_price", "", tp])
    for (metric, period), val in _annual_estimates(fin).items():
        rows.append([TODAY, code, name, wics, metric, period, val])
    return rows, True


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    uni = get_universe_safe()
    log(f"[start] {TODAY} 유니버스 {len(uni)}종목, sleep={SLEEP}s (naver-api v8.1)")

    all_rows = []
    n_ok = n_empty = n_fail = n_op = 0
    sample_fin = None

    for i, row in uni.iterrows():
        code, name = str(row["Code"]), str(row["Name"])
        rows, ok = collect(code, name)
        if not ok:
            n_fail += 1
        elif rows:
            all_rows += rows
            n_ok += 1
            if any(r[4] == "op_e" for r in rows):
                n_op += 1
        else:
            n_empty += 1
            if sample_fin is None:
                sample_fin = fetch_json(f"{BASE}/{code}/finance/annual")

        if (i + 1) % 25 == 0 or (i + 1) == len(uni):
            log(f"  ...{i + 1}/{len(uni)} (ok={n_ok}, op={n_op}, "
                f"empty={n_empty}, fail={n_fail})")
        time.sleep(SLEEP)

    if n_op == 0 and sample_fin is not None:
        log("[warn] 영업이익E 수집 0건 — finance/annual JSON 지문:")
        _fingerprint(sample_fin, "finance/annual")

    df = pd.DataFrame(
        all_rows,
        columns=["run_date", "code", "name", "wics", "metric", "period", "value"],
    )
    if df.empty:
        log("[error] 수집 0건 — 위 [fail-diag]/[json-diag] 참조")
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
        f"/ 실패 {n_fail} / 총 {len(df)}행 / 업종 {n_wics}개 / source=naver-api")


if __name__ == "__main__":
    main()
