# -*- coding: utf-8 -*-
"""
leader_weekly.py — 주간 선봉 리스트 생성기 (로봇 2호)
=====================================================
검증 근거: validate_leader + robust 노트북 (2026-07-11 판정)
  · 선봉픽 H40 동일리그 백분위 98 (반분 99/91 · 2026제외 99)
  · 그리드 8/9칸 85+ · 확정 파라미터 고정 사용
  · 대장 리그는 '깃발'(업종 건강 참고) — 매매 후보는 선봉

동작: m7의 history.csv(WICS 매핑) → FDR 시세 → 업종 내부 EW 모멘텀 top3
      → 리그 채점 → data/leader/ 에 CSV + TV 워치리스트 커밋
실행: GitHub Actions에서 m7 직후 자동 / Colab 수동 폴백 겸용
"""
import os
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ══════════ 검증 확정 파라미터 (임의 변경 금지 — 재검증 대상) ══════════
L1M, L3M, L12M, L52W = 21, 63, 252, 252
LAMT, LAMT_BASE = 20, 60
LEADER_N, MIN_AMT = 5, 100e8
MIN_SECTOR_MEMBERS, TOPK_SECTORS = 8, 3
MIN_FULL_SNAPSHOT = 150          # 유효 스냅샷 판정 (테스트 소형 실행 무시)
WL_SB, WL_DJ = 3, 1              # 워치리스트: 업종당 선봉 3 · 대장(깃발) 1

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime("%Y-%m-%d")
HIST_PATH = os.path.join("data", "m7_revision", "history.csv")
OUT_DIR = os.path.join("data", "leader")


def log(msg):
    print(msg, flush=True)


# ══════════ 1. WICS 매핑 로드 (Actions: repo 경로 / Colab: 업로드) ══════════
if os.path.exists(HIST_PATH):
    hist = pd.read_csv(HIST_PATH, dtype={"code": str})
else:
    from google.colab import files  # Colab 폴백
    log("▶ history.csv 업로드")
    up = files.upload()
    hist = pd.read_csv(list(up.keys())[0], dtype={"code": str})

# 유효(전체 규모) 스냅샷 중 최신 날짜 선택 — top_n=10 테스트 실행 오염 방지
cnt = hist.groupby("run_date")["code"].nunique()
full_dates = cnt[cnt >= MIN_FULL_SNAPSHOT].index
if len(full_dates) == 0:
    log("[error] 전체 규모 스냅샷 없음"); sys.exit(1)
base_date = max(full_dates)
uni = (hist[hist["run_date"] == base_date][["code", "name", "wics"]]
       .drop_duplicates("code"))
log(f"[start] {TODAY} · 매핑 기준일 {base_date} · {len(uni)}종목")

sec_members = uni.groupby("wics")["code"].apply(list)
sec_members = sec_members[sec_members.map(len) >= MIN_SECTOR_MEMBERS]
name_map = dict(zip(uni["code"], uni["name"]))
log(f"리그 성립 업종 {len(sec_members)}개")

# ══════════ 2. 시세 수집 (FDR · 0가격 글리치 정화) ══════════
import subprocess
subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                "finance-datareader"], check=False)
import FinanceDataReader as fdr

start = (datetime.now(KST) - timedelta(days=430)).strftime("%Y-%m-%d")
need = sorted({c for mem in sec_members for c in mem})
frames = {}
for i, c in enumerate(need):
    try:
        d = fdr.DataReader(c, start)
        if d is not None and len(d) > 60:
            frames[c] = d
    except Exception:
        pass
    if (i + 1) % 50 == 0:
        log(f"  ...시세 {i + 1}/{len(need)}")
    time.sleep(0.15)
log(f"시세 확보 {len(frames)}/{len(need)}종목")
if len(frames) < 100:
    log("[error] 시세 수집 부족 — FDR 접근 확인"); sys.exit(1)

codes = list(frames)
C = pd.DataFrame({c: frames[c]["Close"] for c in codes}).sort_index().replace(0, np.nan)
Hi = pd.DataFrame({c: frames[c]["High"] for c in codes}).reindex(C.index).replace(0, np.nan)
A = C * pd.DataFrame({c: frames[c]["Volume"] for c in codes}).reindex(C.index)

r1 = C.pct_change(L1M).iloc[-1]
r3 = C.pct_change(L3M).iloc[-1]
r12 = C.pct_change(L12M).iloc[-1]
prx = (C / Hi.rolling(L52W, min_periods=60).max()).iloc[-1]
a20 = A.rolling(LAMT).mean().iloc[-1]
a60 = A.rolling(LAMT_BASE).mean().iloc[-1]


def prank(s):
    return s.rank(pct=True) * 100


# ══════════ 3. 업종 모멘텀 top3 (내부 EW — 검증과 동일) ══════════
rows = []
for sec, mem in sec_members.items():
    m = [c for c in mem if c in codes and np.isfinite(r3.get(c, np.nan))]
    if len(m) >= MIN_SECTOR_MEMBERS:
        rows.append((sec, np.nanmean(r1[m]), np.nanmean(r3[m]),
                     np.nanmean(r12[m]), m))
sdf = pd.DataFrame(rows, columns=["sec", "s1", "s3", "s12", "mem"])
sdf["sc"] = (prank(sdf.s1) + prank(sdf.s3) + prank(sdf.s12)) / 3
top = sdf.sort_values("sc", ascending=False).head(TOPK_SECTORS)
log("주도 업종 top3: " + " / ".join(
    f"{r.sec}({r.sc:.0f})" for r in top.itertuples()))

# ══════════ 4. 리그 채점 ══════════
out = []
for srow in top.itertuples():
    m = srow.mem
    g = pd.DataFrame({
        "code": m,
        "exc1": [r1[c] - srow.s1 for c in m],
        "exc3": [r3[c] - srow.s3 for c in m],
        "prox": [prx[c] for c in m],
        "a20":  [a20[c] for c in m],
        "shD":  [(a20[c] / np.nansum(a20[m])) - (a60[c] / np.nansum(a60[m]))
                 for c in m],
    }).dropna()
    if len(g) < MIN_SECTOR_MEMBERS - 2:
        continue
    g = g.sort_values("a20", ascending=False).reset_index(drop=True)

    def scored(league, tag):
        if len(league) < 2:
            return league.assign(score=50, league=tag)
        sc = ((prank(league.exc1) + prank(league.exc3)) / 2
              + prank(league.prox) + prank(league.shD)) / 3
        return (league.assign(score=sc.round(0).astype(int), league=tag)
                .sort_values("score", ascending=False))

    dj = scored(g.head(LEADER_N), "대장(깃발)")
    sb = g.iloc[LEADER_N:]
    sb = scored(sb[sb["a20"] >= MIN_AMT], "선봉")
    for part in (dj, sb):
        part = part.assign(sector=srow.sec, name=[name_map.get(c, "") for c in part["code"]])
        out.append(part)

res = pd.concat(out, ignore_index=True)
res["run_date"] = TODAY
res = res[["run_date", "sector", "league", "code", "name",
           "exc1", "exc3", "prox", "a20", "shD", "score"]]

# ══════════ 5. 저장: CSV + TV 워치리스트 + 사람용 요약 ══════════
os.makedirs(OUT_DIR, exist_ok=True)
res.to_csv(os.path.join(OUT_DIR, f"{TODAY}.csv"), index=False, encoding="utf-8-sig")
res.to_csv(os.path.join(OUT_DIR, "latest.csv"), index=False, encoding="utf-8-sig")

# 사람용 요약.md — 폰 GitHub 앱에서 표로 렌더링되는 판
md = [f"# 주간 선봉 리포트 — {TODAY}", ""]
picks = []
for srow in top.itertuples():
    sec = srow.sec
    r = res[res["sector"] == sec]
    sb = r[r["league"] == "선봉"]
    if len(sb):
        b = sb.iloc[0]
        tags = []
        if len(sb) < 5:
            tags.append("⚠소표본")
        if b["exc3"] <= 0:
            tags.append("🚩초과음수(나쁜무리1등)")
        tag = (" " + "·".join(tags)) if tags else ""
        picks.append(f"**{b['name']}** ({sec}, 점수 {b['score']}, "
                     f"초과3M {b['exc3']*100:+.0f}%){tag}")
md.append("## 이번 주 픽 후보 (검증 원형: 리그 top1 전원 — 꼬리표는 경고)")
md += [f"- {p}" for p in picks] if picks else ["- (해당 없음)"]
md.append("")
for srow in top.itertuples():
    sec = srow.sec
    r = res[res["sector"] == sec]
    for lg, title in (("선봉", "선봉 (매매 후보)"), ("대장(깃발)", "대장 — 깃발(참고)")):
        g = r[r["league"] == lg].head(6)
        if g.empty:
            continue
        warn = " ⚠소표본(순위 신뢰 낮음)" if lg == "선봉" and len(r[r["league"] == lg]) < 5 else ""
        md.append(f"## {sec} · {title}{warn}")
        md.append("| 종목 | 초과1M | 초과3M | 52주% | 대금(억) | Δ점유 | 점수 |")
        md.append("|---|---|---|---|---|---|---|")
        for _, x in g.iterrows():
            md.append(f"| {x['name']} | {x['exc1']*100:+.1f}% | {x['exc3']*100:+.1f}% "
                      f"| {x['prox']*100:.0f} | {x['a20']/1e8:,.0f} "
                      f"| {x['shD']*100:+.2f} | **{x['score']}** |")
        md.append("")
md.append("> 읽기 규칙: 점수는 리그 내 상대평가 — 반드시 초과3M과 함께 볼 것. "
          "100점이어도 초과3M 음수면 '나쁜 무리의 1등'. 검증 지평 H40(~8주).")
with open(os.path.join(OUT_DIR, "요약.md"), "w", encoding="utf-8") as f:
    f.write("\n".join(md))

wl_lines = []
for sec in top["sec"]:
    r = res[res["sector"] == sec]
    sb = r[r["league"] == "선봉"].head(WL_SB)
    dj = r[r["league"] == "대장(깃발)"].head(WL_DJ)
    wl_lines.append(f"###{sec}·선봉")
    wl_lines += [f"KRX:{c}" for c in sb["code"]]
    wl_lines.append(f"###{sec}·깃발")
    wl_lines += [f"KRX:{c}" for c in dj["code"]]
with open(os.path.join(OUT_DIR, "watchlist_tv.txt"), "w", encoding="utf-8") as f:
    f.write(",".join(wl_lines))

# 로그 요약
for sec in top["sec"]:
    r = res[res["sector"] == sec]
    sb = r[r["league"] == "선봉"].head(3)
    dj = r[r["league"] == "대장(깃발)"].head(1)
    log(f"\n[{sec}] 깃발: " + ", ".join(
        f"{n}({s})" for n, s in zip(dj["name"], dj["score"])))
    log(f"        선봉: " + ", ".join(
        f"{n}({s})" for n, s in zip(sb["name"], sb["score"])))
log(f"\n[done] {TODAY}: {len(res)}행 저장 · data/leader/latest.csv · watchlist_tv.txt")
