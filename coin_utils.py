from __future__ import annotations

import requests
import pandas as pd
import streamlit as st

try:
  import pybithumb
  _PYBITHUMB_OK = True
except ImportError:
  _PYBITHUMB_OK = False

# 빗썸 공개 REST API 엔드포인트
_BITHUMB_TICKER_ALL = "https://api.bithumb.com/public/ticker/ALL_KRW"


@st.cache_data(ttl=3600)
def get_coin_listing(min_volume_krw: int = 10_000_000_000) -> pd.DataFrame:
  """빗썸 전체 KRW 마켓 코인 목록 조회.

  - 24시간 거래대금(KRW) 기준으로 min_volume_krw 이상인 코인만 반환
  - 반환 컬럼: ticker(str), volume_24h(float), close(float)
  - volume_24h 내림차순 정렬
  """
  try:
    resp = requests.get(_BITHUMB_TICKER_ALL, timeout=10)
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("status") != "0000":
      return pd.DataFrame()

    data: dict[str, object] = payload.get("data", {})
    rows: list[dict[str, object]] = []

    for ticker, info in data.items():
      # "date" 키는 타임스탬프 메타데이터 — 건너뜀
      if ticker == "date":
        continue
      if not isinstance(info, dict):
        continue

      try:
        volume_24h = float(info.get("acc_trade_value_24H", 0) or 0)
        close = float(info.get("closing_price", 0) or 0)
      except (ValueError, TypeError):
        continue

      if volume_24h >= min_volume_krw:
        rows.append({
          "ticker": ticker,
          "volume_24h": volume_24h,
          "close": close,
        })

    if not rows:
      return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["ticker", "volume_24h", "close"])
    df = df.sort_values("volume_24h", ascending=False).reset_index(drop=True)
    return df

  except Exception:
    return pd.DataFrame()


@st.cache_data(ttl=3600)
def get_ohlcv_coin(ticker: str, count: int = 200) -> pd.DataFrame:
  """빗썸 일봉 OHLCV 데이터 조회.

  - pybithumb.get_candlestick(ticker, "KRW", "24h") 사용
  - 반환 컬럼: open, high, low, close, volume
  - 인덱스: datetime
  """
  if not _PYBITHUMB_OK:
    return pd.DataFrame()

  try:
    # pybithumb 반환 컬럼 순서: open, close, high, low, volume
    raw: pd.DataFrame = pybithumb.get_candlestick(ticker, "KRW", "24h")
    if raw is None or raw.empty:
      return pd.DataFrame()

    df = raw.tail(count).copy()

    # 컬럼 순서 재정렬: open, high, low, close, volume
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    return df

  except Exception:
    return pd.DataFrame()


@st.cache_data(ttl=3600)
def get_btc_direction() -> str:
  """BTC 시장 방향 판단.

  - 상승: close > MA20 AND close > MA60 AND MA20 > MA60
  - 하락: close < MA60
  - 중립: 그 외
  """
  try:
    df = get_ohlcv_coin("BTC", 200)
    if df.empty or len(df) < 60:
      return "중립"

    df = add_moving_averages(df)
    last = df.iloc[-1]

    close = float(last["close"])
    ma20 = float(last["MA20"])
    ma60 = float(last["MA60"])

    if close > ma20 and close > ma60 and ma20 > ma60:
      return "상승"
    elif close < ma60:
      return "하락"
    else:
      return "중립"

  except Exception:
    return "중립"


def add_moving_averages(df: pd.DataFrame) -> pd.DataFrame:
  """이동평균선 컬럼 추가.

  - 추가 컬럼: MA5, MA20, MA60, MA120
  - "close" 컬럼 기준으로 rolling mean 계산
  """
  df = df.copy()
  close = df["close"]
  df["MA5"] = close.rolling(5).mean()
  df["MA20"] = close.rolling(20).mean()
  df["MA60"] = close.rolling(60).mean()
  df["MA120"] = close.rolling(120).mean()
  return df


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
  """ATR(Average True Range) 컬럼 추가.

  - True Range = max(high-low, |high-prev_close|, |low-prev_close|)
  - Wilder EWM: ewm(alpha=1/period, adjust=False).mean()
  """
  df = df.copy()
  high = df["high"]
  low = df["low"]
  close = df["close"]
  prev_close = close.shift(1)

  tr = pd.concat([
    high - low,
    (high - prev_close).abs(),
    (low - prev_close).abs(),
  ], axis=1).max(axis=1)

  df["ATR"] = tr.ewm(alpha=1 / period, adjust=False).mean()
  return df


def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
  """RSI(Relative Strength Index) 컬럼 추가.

  - "close" 컬럼 기준
  - Wilder EWM 방식으로 계산
  """
  df = df.copy()
  delta = df["close"].diff()

  gain = delta.clip(lower=0)
  loss = (-delta).clip(lower=0)

  avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
  avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

  rs = avg_gain / avg_loss.replace(0, float("nan"))
  df["RSI"] = 100 - (100 / (1 + rs))
  return df
