# K-Quant Streamlit Tracker

한국 주식 + 빗썸 코인 MA 눌림목 스캐너 및 수동매매 신호 생성 도구

---

## 모듈 구조

### 주식 (K-Quant)
```
app.py        # Streamlit 진입점, 탭 라우팅 (스캐너 / 백테스트 / 검증 / 히스토리)
scanner.py    # 종목 스캔, 조건 필터링, _select_best3 가중치 순위화
utils.py      # 공통 유틸 (OHLCV 조회, MA/ATR/RSI 계산, 수급 데이터)
backtest.py   # 과거 데이터 기반 전략 성과 검증
sheets.py     # Google Sheets 스캔 결과 저장 / 히스토리 조회 (history 시트)
config.yaml   # 주식 전략 파라미터 중앙 관리
```

### 코인 (Coin Quant) — 주식 모듈과 완전 독립
```
coin_app.py      # 코인 Streamlit 앱 진입점 (단기 / 스윙 / 검증 3탭)
coin_scanner.py  # 코인 종목 스캔 — MA 눌림목 필터, BTC 방향 필터, 가중치 스코어링
coin_utils.py    # 코인 데이터 레이어 — 빗썸 REST API, pybithumb OHLCV, MA/ATR/RSI
coin_sheets.py   # 코인 Google Sheets 연동 — coin_history 시트 저장/판정 (주식 시트 불변)
coin_config.yaml # 코인 전략 파라미터 (config.yaml과 완전 분리)
```

### 데이터 흐름 — 주식

```
get_stock_listing()           # KRX 상장 종목 목록 + 시가총액
  └─ get_ohlcv()              # OHLCV 일봉 (FinanceDataReader)
       ├─ add_moving_averages() # MA5 / MA20 / MA60 / MA120
       ├─ add_atr()            # ATR(14) — 익절/손절 계산
       └─ add_rsi()            # RSI(14) — 구간 점수화
  └─ get_investor_flow()      # 기관/외국인 순매수일 (최근 3일)
```

### 데이터 흐름 — 코인

```
get_btc_direction()           # BTC MA20/MA60 기준 시장 방향 ('상승'일 때만 스캔 진행)
get_coin_listing()            # 빗썸 REST API — KRW 마켓 전종목 + 24h 거래대금 필터
  └─ get_ohlcv_coin()         # 일봉 OHLCV (pybithumb)
       ├─ add_moving_averages() # MA5 / MA20 / MA60 / MA120
       ├─ add_atr()            # ATR(14) — 코인 변동성 반영 익절/손절
       └─ add_rsi()            # RSI(14) — 구간 점수화
```

---

## 스캔 전략

### 주식 단기 눌림목 (`scan_day_trading`)
| 조건 | 내용 |
|------|------|
| ① 중기 추세 | 종가 > MA60, MA60 우상향 |
| ② 눌림 확인 | 최근 3일 중 하락일 ≥ 2 |
| ③ 지지선 근접 | 종가 MA20 대비 -3% ~ +1% |
| ④ MA20 우상향 | ma20[-1] > ma20[-2] (필수) |
| ⑤ 거래량 감소 | 3일 평균 < 직전 20일 평균 × 0.7 |
| ⑥ 수급 | 기관 or 외국인 순매수 ≥ 2일 |

### 주식 스윙 눌림목 (`scan_swing`)
| 조건 | 내용 |
|------|------|
| ① 장기 추세 | 종가 > MA120, MA120 우상향 |
| ② 눌림 확인 | 최근 5일 중 하락일 ≥ 3 |
| ③ 지지선 근접 | 종가 MA60 대비 -3% ~ +1% |
| ④ MA20 우상향 | ma20[-1] > ma20[-2] (필수) |
| ⑤ 거래량 감소 | 5일 평균 < 직전 20일 평균 × 0.7 |
| ⑥ 수급 | 기관 or 외국인 순매수 ≥ 2일 |

---

### 코인 단기 눌림목 (`scan_coin_day`) — BTC 상승 추세일 때만 실행
| 조건 | 내용 |
|------|------|
| ✦ BTC 필터 | BTC > MA20 AND BTC > MA60 AND MA20 > MA60 (전제 조건) |
| ① 추세 | 종가 > MA20 > MA60 |
| ② MA20 우상향 | ma20[-1] > ma20[-2] |
| ③ 눌림 확인 | 최근 3일 중 하락일 ≥ 2 |
| ④ 지지선 근접 | 종가 MA20 대비 -6% ~ +1.8% |
| ⑤ 고점 대비 하락 | 최근 20일 고점 대비 ≤ -10% |
| ⑥ 거래량 수축 | 3일 평균 < 직전 20일 평균 × 0.7 |
| ⑦ RSI 과매수 제외 | RSI ≤ 75 |

**진입/청산**: 현재 종가 기준 즉시 진입 / TP = 종가 + ATR × 4.0 / SL = 종가 − ATR × 2.0 / 최대 보유 5일

### 코인 스윙 눌림목 (`scan_coin_swing`) — BTC 상승 추세일 때만 실행
| 조건 | 내용 |
|------|------|
| ✦ BTC 필터 | BTC > MA20 AND BTC > MA60 AND MA20 > MA60 (전제 조건) |
| ① 추세 | 종가 > MA60 > MA120 |
| ② MA60 우상향 | ma60[-1] > ma60[-2] |
| ③ 눌림 확인 | 최근 5일 중 하락일 ≥ 4 |
| ④ 지지선 근접 | 종가 MA60 대비 -10% ~ +3% |
| ⑤ 고점 대비 하락 | 최근 20일 고점 대비 ≤ -15% |
| ⑥ 거래량 수축 | 5일 평균 < 직전 20일 평균 × 0.7 |
| ⑦ RSI 과매수 제외 | RSI ≤ 75 |

**진입/청산**: 현재 종가 기준 즉시 진입 / TP = 종가 + ATR × 6.0 / SL = 종가 − ATR × 3.0 / 최대 보유 10일

---

## 가중치 점수

### 주식 (`_select_best3`)

| 항목 | 가중치 | 산식 |
|------|--------|------|
| 수급 | 30% | `(inst_days + foreign_days) / 6 × 30` |
| 손익비 | 25% | `risk_reward.clip(3.0) / 3.0 × 25` |
| 눌림률 | 15% | `(1 - pullback_pct.abs() / 5) × 15` |
| 추세 품질 | 20% | `vol_consec_drop(bool) × 20` |
| RSI 구간 | 10% | `rsi_score / 10 × 10` |
| **합계** | **100%** | |

**RSI 구간 점수**: 40~55 → 10점 / 30~40 → 5점 / 그 외 → 0점

### 코인 (`_select_best3_coin`)

수급 데이터 없음 → 거래대금 순위 + 거래량 강도로 대체

| 항목 | 가중치 | 산식 |
|------|--------|------|
| 거래대금 순위 | 20% | 상위 20%→20점 / 40%→15점 / 60%→10점 / 나머지→5점 |
| 손익비 | 25% | `risk_reward.clip(4.0) / 4.0 × 25` |
| 눌림률 | 15% | `(1 - pullback_pct.abs() / pullback_band) × 15` |
| 추세 품질 | 15% | `vol_consec_drop(bool) × 15` |
| RSI 구간 | 10% | 40~60→10점 / 30~40 or 60~70→5점 / 그 외→0점 |
| 거래량 강도 | 15% | `vol_decay_score(bool) × 15` |
| **합계** | **100%** | |

---

## 상수 관리

- **주식**: `config.yaml` — scanner.py, backtest.py에서 로드
- **코인**: `coin_config.yaml` — coin_scanner.py 내 `_CFG` dict로 직접 관리 (yaml 의존성 제거)

---

## Google Sheets 연동

| 시트명 | 용도 | 관리 모듈 |
|--------|------|-----------|
| `history` | 주식 스캔 결과 저장/조회 | sheets.py |
| `coin_history` | 코인 스캔 결과 저장/조회 | coin_sheets.py |

---

## 진척도 관리표

### 주식 (K-Quant)

| # | 작업 | 상태 | 커밋 |
|---|------|------|------|
| 1 | MA20 우상향 필터 추가 (필수 조건) | ✅ 완료 | `c605300` |
| 2 | RSI 구간 점수화 (add_rsi 활용) | ✅ 완료 | `c605300` |
| 3 | 거래량 연속 감소 패턴 (vol_consec_drop) | ✅ 완료 | `c605300` |
| 4 | _select_best3 가중치 재설계 (5항목) | ✅ 완료 | `c605300` |
| 5 | README.md 및 config.yaml 생성 | ✅ 완료 | — |
| 6 | 백테스트-스캐너 로직 통일 | ⏳ 대기 | 데이터 충분 시 |
| 7 | 파라미터 슬라이더 UI | ⏳ 대기 | 데이터 충분 시 |

### 코인 (Coin Quant)

| # | 작업 | 상태 | 커밋 |
|---|------|------|------|
| 8 | 코인 단기·스윙·검증 탭 추가 (app.py 통합) | ✅ 완료 | `62f3654` |
| 9 | coin_scanner 로드 오류 수정 (경로 절대화) | ✅ 완료 | `ae01c33` |
| 10 | coin_scanner yaml 의존성 제거 → Python dict | ✅ 완료 | `f18e49c` |
| 11 | 코인 스캔 대상 거래대금 완화 (100억 → 5억) | ✅ 완료 | `c4a9434` |
| 12 | 코인 진입 타이밍 수정 (다음날 시초가 → 당일 종가 즉시) | ✅ 완료 | `fe74654` |
| 13 | 코인 자동화 (빗썸 API 자동매매) | ⏳ 대기 | 30건 검증 후 |
