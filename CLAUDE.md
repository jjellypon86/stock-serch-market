# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 실행 명령어

```bash
# 앱 실행
streamlit run app.py

# 패키지 설치
pip install -r requirements.txt
```

테스트 파일 없음. 기능 검증은 Streamlit Cloud 배포 후 UI에서 직접 확인.

## 모듈 구조

```
app.py        # Streamlit 진입점 — 4탭 UI (단기/스윙/백테스트/검증), 스캔 결과 저장 호출
scanner.py    # 종목 스캔 핵심 로직 — MA20/MA60 눌림목 필터링, 가중치 스코어링, 진입/청산가 산출
utils.py      # 데이터 레이어 — OHLCV 조회, Naver 스크래핑, 이동평균·ATR·RSI 계산
backtest.py   # 백테스트 엔진 — 랜덤 200종목 샘플, 슬리피지 반영 시뮬레이션, MDD 산출
sheets.py     # Google Sheets 연동 — 스캔 결과 저장, WIN/LOSS/EXPIRED 자동 판정, 전략 평가
config.yaml   # 전략 파라미터 중앙 관리 — 시장 필터, 스코어 가중치, 백테스트 설정
```

## 아키텍처 개요

```
app.py
  ├── scan_day_trading() / scan_swing()    ← scanner.py
  │       └── get_stock_listing()          ← utils.py → Naver Finance 스크래핑
  │           get_ohlcv()                  ← utils.py → FinanceDataReader
  │           get_investor_flow()          ← utils.py → Naver Finance 스크래핑
  │           add_moving_averages/atr/rsi()← utils.py
  ├── run_backtest()                        ← backtest.py
  │       └── (동일 utils.py 사용)
  ├── save_scan_results()                   ← sheets.py → Google Sheets API
  └── update_results() / evaluate_strategy()← sheets.py
```

`config.yaml`은 scanner.py와 backtest.py에서 직접 로드하여 파라미터로 사용.

## 핵심 전략

### 단기 (MA20 눌림목, `scan_day_trading`)
7가지 필터를 모두 통과해야 후보 등록:
1. 종가 > MA20 > MA60 (상승 추세)
2. MA20 우상향 (전일 대비 상승)
3. 최근 3일 중 하락일 ≥ 2 (눌림목 발생)
4. `|종가 - MA20| / MA20 ≤ 3%` (지지선 근접)
5. 3일 평균 거래량 < 20일 평균 × 0.7 (거래량 수축)
6. 기관 또는 외국인 순매수 최근 3일 중 ≥ 2일 (수급)
7. RSI ≤ 70 (과매수 제거), 고점 대비 하락률 ≥ 5%

### 스윙 (MA60 눌림목, `scan_swing`)
단기와 동일 구조, 다른 파라미터:
- 기준 이동평균: MA60 > MA120
- 눌림목 윈도우: 5일 중 하락일 ≥ 3
- 거래량 수축: 5일 평균 < 20일 평균 × 0.7

### 종목 선정 (`_select_best3`)
후보 중 상위 3개를 가중치 스코어로 선정:

| 항목 | 가중치 |
|------|--------|
| 수급 (기관+외국인 순매수일) | 30% |
| 리스크/리워드 비율 | 25% |
| 추세 품질 (거래량 연속 감소) | 20% |
| 눌림목 강도 | 15% |
| RSI 밴드 스코어 | 10% |

진입가: 당일 종가 / 목표가: 종가 + ATR × tp_mult / 손절가: 종가 − ATR × sl_mult

## 주요 함수 참조

**scanner.py**
- `scan_day_trading(date, market)` → 단기 상위 3종목 DataFrame
- `scan_swing(end_date, market)` → 스윙 상위 3종목 DataFrame
- `_select_best3(df)` → 가중치 스코어링 후 상위 3개 반환
- `_calc_exit_prices(close, atr, tp_mult, sl_mult)` → (목표가, 손절가, RR)

**utils.py**
- `get_stock_listing(market)` → 시가총액·거래대금 필터링된 종목 목록
- `get_ohlcv(ticker, start, end)` → OHLCV DataFrame (컬럼명: 시가/고가/저가/종가/거래량)
- `get_investor_flow(ticker)` → `{"기관_순매수일": int, "외국인_순매수일": int}`
- `get_market_direction(date)` → KOSPI vs MA20 추세 dict
- `add_moving_averages/add_atr/add_rsi(df)` → 지표 컬럼 추가

**backtest.py**
- `run_backtest(strategy, months, market)` → 백테스트 결과 dict
- `_simulate_trade(df, entry_idx, tp_pct, sl_pct, max_hold)` → 단일 거래 시뮬레이션

**sheets.py**
- `save_scan_results(df, strategy, market, scan_date)` → Sheets 저장 (중복 체크 포함)
- `update_results()` → OHLCV로 WIN/LOSS/EXPIRED 자동 판정
- `evaluate_strategy(df_done)` → 0~100점 전략 평가 + 합격/경고/재검토 판정
- `load_history()` → 전체 히스토리 DataFrame (캐시 300s)

## 외부 의존성 & 시크릿

**데이터 소스**
- **Naver Finance** (`finance.naver.com`) — 종목 목록, 투자자 수급, 뉴스 (BeautifulSoup 스크래핑)
- **FinanceDataReader** — OHLCV 데이터
- **Google Sheets API** — 매매 일지 저장/조회

**시크릿 (Streamlit Cloud)**
- `st.secrets["gcp_service_account"]` — Google Sheets 접근용 서비스 계정 JSON

## 개발 지침

- `@st.cache_data(ttl=...)` 로 모든 외부 호출 캐싱: OHLCV·수급·종목목록 3600s, 뉴스 1800s, 종목명 86400s, Sheets 300s
- FinanceDataReader는 장 마감(15:30 KST) 이후 당일 데이터 조회 가능 — 장중 실시간 데이터 미지원
- `get_ohlcv()` 의 날짜 입력 형식: YYYYMMDD → 내부에서 YYYY-MM-DD로 변환
- OHLCV 컬럼명은 한국어: `시가`, `고가`, `저가`, `종가`, `거래량`
- 수익률 계산 시 수수료(0.015%) + 증권거래세(0.2%) 반영
- 백테스트 생존 편향 제거: 랜덤 200종목 샘플링 (seed=42 고정)
- 타입 힌트 명시, `any` 타입 사용 금지
