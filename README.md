krx-cycle-data
산업 사이클 분석용 데이터 수집 파이프라인.
현재 가동 모듈: m7 — FnGuide 컨센서스 주간 스냅샷 (리비전 시계열 축적).
컨센서스는 과거 소급이 불가능하므로, 이 repo가 켜진 날부터 시계열이 시작된다.
부수입으로 WICS 업종 분류가 함께 수집되어 산업→종목 매핑테이블이 된다.
---
최초 설정 (PC 권장, 약 15분, 1회만)
1. GitHub 계정 생성
github.com → Sign up → 이메일 인증까지.
2. Repo 생성
우측 상단 `+` → New repository
Repository name: `krx-cycle-data`
Private 선택 (무료로 충분: Actions 월 2,000분 제공, 이 파이프라인은 월 ~30분 사용)
Create repository
3. 파일 3개 업로드
황금률: 파일을 만들 때마다 반드시 좌측 상단 Code 탭을 먼저 눌러 루트로 돌아온 뒤 시작한다.
(폴더 안에서 Create new file을 누르면 그 폴더 하위에 생겨버린다.)
루트에서 Add file → Create new file 클릭 후,
파일명 칸에 아래 경로를 그대로 입력하면 폴더가 자동 생성된다.
각 파일 내용을 붙여넣고 Commit changes.
순서	파일명 칸에 입력할 경로	내용
1	`README.md`	이 파일
2	`m7_fnguide.py`	스크래퍼
3	`.github/workflows/m7_weekly.yml`	자동 실행 스케줄
4. 첫 실행 (수동 테스트)
상단 Actions 탭 → 워크플로우 활성화 버튼이 보이면 클릭 →
좌측 `m7-fnguide-weekly` → 우측 Run workflow → Run.
약 5~8분 소요. 초록 체크가 뜨면 성공.
5. 결과 확인
repo의 `data/m7_revision/` 폴더에
`YYYY-MM-DD.csv` — 당일 스냅샷
`history.csv` — 누적본
이 자동 커밋되어 있으면 완료.
---
이후 운영
자동: 매주 토요일 07:00 KST에 스스로 돈다. 아무것도 안 해도 됨.
수동: 모바일 GitHub 앱에서도 Actions → Run workflow 가능.
주의: GitHub는 repo가 60일간 활동이 없으면 schedule을 끈다.
주간 커밋이 자동으로 발생하므로 보통 문제없지만, 실패가 이어지면 꺼질 수 있으니
한 달에 한 번쯤 Actions 탭에 초록불이 이어지는지 확인.
데이터 스키마
`run_date, code, name, wics, metric, period, value` (long format)
metric	의미	단위
`op_e`	영업이익 컨센서스 (연간 추정)	억원
`rev_e`	매출액 컨센서스 (연간 추정)	억원
`target_price`	목표주가	원
`period`는 `2026/12` 형식의 회계연도. `wics`는 FnGuide WICS 업종명.
리비전 계산(추후 Colab): 종목별 `op_e`의 4주/12주 변화율 → WICS 업종별 중앙값 집계.
실패 시 폴백
Actions 로그에 `수집 0건`이 찍히면 FnGuide가 해외 IP(GitHub 서버)를 차단한 경우다.
PC(국내 IP)에서 로컬 실행:
```
pip install requests pandas lxml html5lib beautifulsoup4 finance-datareader
python m7_fnguide.py
```
생성된 `data/m7_revision/` 안 CSV를 repo에 수동 업로드(Add file → Upload files)하면
시계열은 끊기지 않는다.
환경변수 (선택)
`M7_TOP_N` : 유니버스 크기 (기본 300)
`M7_SLEEP` : 요청 간격 초 (기본 0.7 — 서버 예의상 낮추지 말 것)
로드맵
m1 업종지수 모멘텀 / m2 금리·환율·위험 (FRED+ECOS) / m3 외국인 수급
m4 KOSIS 재고·가동률 / m5 관세청 10일 수출 / m6 수출물가지수
m8 DART capex 파싱
최종 산출: 주간 산업 국면 상태표 → 스캐너 앞단 유니버스 필터
