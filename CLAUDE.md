# 프로젝트: K-Quant Streamlit Tracker

## 기술 스택
- 언어: Python 3.10+
- 프레임워크: Streamlit
- 주요 라이브러리: finance-datareader, pandas-ta, plotly, pandas
- 환경: macOS, PyCharm

## 모듈 구조
```
app.py        # Streamlit 진입점, 페이지 라우팅
scanner.py    # 종목 스캔 및 조건 필터링 로직
utils.py      # 공통 함수 (날짜 처리, 지표 계산 등)
```

## 핵심 전략

### 단기 (당일 매매)
- 목표 수익: 5%+
- 조건: 거래량 급등 (전일 대비 200%+), RSI 반전, 변동성 돌파

### 스윙 (1주일)
- 목표 수익: 3~7%
- 조건: 5/20일 골든크로스, 기관/외국인 순매수 3일 연속

## 개발 지침
- `st.cache_data` 데코레이터로 FinanceDataReader 호출 캐싱 (동일 데이터 중복 요청 방지)
- UI는 `st.metric`, `st.columns` 기반 모바일 우선 설계
- FinanceDataReader는 장 마감(15:30) 이후 당일 데이터 조회 가능 — 장중 실시간 데이터 미지원
- API 실패 시 빈 DataFrame 반환하고 `st.warning`으로 사용자에게 알림
- 수익률 계산 시 매매 수수료(0.015%) 및 증권거래세(0.2%) 반영

## 코딩 스타일
- 들여쓰기: 4칸 (Python 표준)
- 함수명/변수명: snake_case
- 주석: 한국어
- `any` 타입 사용 금지, 타입 힌트 명시
