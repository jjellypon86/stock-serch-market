from __future__ import annotations

import pandas as pd
import streamlit as st

from coin_utils import (
  add_atr,
  add_moving_averages,
  add_rsi,
  get_btc_direction,
  get_coin_listing,
  get_ohlcv_coin,
)

# 전략 파라미터 (yaml 의존성 제거 — 외부 패키지 없이 동작)
_CFG: dict[str, object] = {
  "strategy": {
    "day": {
      "ma_short": 20,
      "ma_long": 60,
      "pullback_window": 3,
      "pullback_band": 6.0,
      "vol_decay_ratio": 0.70,
      "rsi_overbought": 75,
      "drawdown_from_high": -10.0,
      "tp_mult": 4.0,
      "sl_mult": 2.0,
      "max_hold_days": 5,
    },
    "swing": {
      "ma_short": 60,
      "ma_long": 120,
      "pullback_window": 5,
      "pullback_band": 10.0,
      "vol_decay_ratio": 0.70,
      "rsi_overbought": 75,
      "drawdown_from_high": -15.0,
      "tp_mult": 6.0,
      "sl_mult": 3.0,
      "max_hold_days": 10,
    },
  },
  "market": {
    "min_24h_volume_krw": 10_000_000_000,
    "exclude_tickers": ["BTC"],
  },
  "fee": {
    "bithumb_taker": 0.0025,
    "slippage": 0.001,
  },
}


def _count_down_days(df: pd.DataFrame, window: int) -> int:
  """최근 window일 중 하락일(close < 전일 close) 수 반환"""
  closes = df["close"].iloc[-(window + 1):]
  return sum(
    1 for i in range(1, len(closes))
    if closes.iloc[i] < closes.iloc[i - 1]
  )


def _calc_exit_prices_coin(
  close: float,
  atr: float,
  tp_mult: float,
  sl_mult: float,
) -> tuple[float, float, float]:
  """목표가·손절가·손익비 계산 (코인은 소수점 그대로 반환)"""
  tp = close + atr * tp_mult
  sl = close - atr * sl_mult
  rr = round((tp - close) / (close - sl), 2) if close > sl else 0.0
  return tp, sl, rr


def _select_best3_coin(df: pd.DataFrame) -> pd.DataFrame:
  """가중치 스코어링으로 상위 3개 코인 반환.

  가중치 합계 100점:
  - 거래대금 순위 (20점)
  - 손익비       (25점)
  - 눌림률       (15점)
  - 추세 품질    (15점)
  - RSI 구간     (10점)
  - 거래량 강도  (15점)
  """
  if df.empty:
    return df

  df = df.copy()

  # 거래대금 순위 점수 (내림차순 백분위)
  rank = df["volume_24h"].rank(ascending=False, pct=True)
  vol_rank_score = rank.apply(
    lambda r: 20 if r <= 0.2 else 15 if r <= 0.4 else 10 if r <= 0.6 else 5
  )

  # RSI 구간 점수
  rsi_score = df["rsi"].apply(
    lambda r: 10 if 40 <= r <= 60 else 5 if (30 <= r < 40 or 60 < r <= 70) else 0
  )

  df["_score"] = (
    vol_rank_score
    + df["risk_reward"].clip(upper=4.0) / 4.0 * 25
    + (1 - df["pullback_pct"].abs() / df["pullback_band"]).clip(lower=0) * 15
    + df["vol_consec_drop"].astype(int) * 15
    + rsi_score
    + df["vol_decay_score"].astype(int) * 15
  )

  return df.nlargest(3, "_score").drop(columns="_score").reset_index(drop=True)


def scan_coin_day() -> pd.DataFrame:
  """단기 코인 눌림목 스캔 (MA20 기준).

  7가지 필터를 모두 통과한 종목에 가중치 스코어를 적용해 상위 3개 반환.
  BTC 시장 방향이 '상승'이 아니면 빈 DataFrame 반환.
  """
  # BTC 시장 방향 확인
  if get_btc_direction() != "상승":
    return pd.DataFrame()

  cfg = _CFG["strategy"]["day"]
  ma_short: int = cfg["ma_short"]          # 20
  ma_long: int = cfg["ma_long"]            # 60
  window: int = cfg["pullback_window"]     # 3
  band: float = cfg["pullback_band"]       # 6.0
  vol_ratio: float = cfg["vol_decay_ratio"]  # 0.70
  rsi_ob: float = cfg["rsi_overbought"]    # 75
  drawdown_thresh: float = cfg["drawdown_from_high"]  # -10.0
  tp_mult: float = cfg["tp_mult"]
  sl_mult: float = cfg["sl_mult"]

  # 거래대금 필터 통과 코인 목록 조회
  listing_df = get_coin_listing(min_volume_krw=_CFG["market"]["min_24h_volume_krw"])
  if listing_df.empty:
    return pd.DataFrame()

  # 제외 티커 필터링 (BTC 등)
  exclude: list[str] = _CFG["market"].get("exclude_tickers", [])
  listing_df = listing_df[~listing_df["ticker"].isin(exclude)].reset_index(drop=True)
  if listing_df.empty:
    return pd.DataFrame()

  # volume_24h 빠른 접근용 dict
  listing_map: dict[str, dict[str, float]] = {
    row["ticker"]: {"volume_24h": row["volume_24h"]}
    for _, row in listing_df.iterrows()
  }

  tickers: list[str] = listing_df["ticker"].tolist()
  results: list[dict[str, object]] = []

  # Streamlit progress bar (Streamlit 환경이 아닐 경우 안전 처리)
  try:
    progress = st.progress(0, text="코인 단기 스캔 중...")
    _has_progress = True
  except Exception:
    _has_progress = False

  for i, ticker in enumerate(tickers):
    try:
      if _has_progress:
        progress.progress(
          (i + 1) / len(tickers),
          text=f"분석 중: {ticker} ({i + 1}/{len(tickers)})",
        )

      df = get_ohlcv_coin(ticker, count=200)
      # MA120 + 여유 확보 최소 130봉 필요
      if df.empty or len(df) < 130:
        continue

      df = add_moving_averages(df)
      df = add_atr(df)
      df = add_rsi(df)
      df = df.dropna(subset=[f"MA{ma_short}", f"MA{ma_long}", "ATR", "RSI"])

      if len(df) < window + 2:
        continue

      close = float(df["close"].iloc[-1])
      ma20 = float(df[f"MA{ma_short}"].iloc[-1])
      ma60 = float(df[f"MA{ma_long}"].iloc[-1])
      ma20_prev = float(df[f"MA{ma_short}"].iloc[-2])
      atr = float(df["ATR"].iloc[-1])
      rsi = float(df["RSI"].iloc[-1])

      # 필터 1: 추세 (close > MA20 > MA60)
      if not (close > ma20 > ma60):
        continue

      # 필터 2: MA20 우상향
      if ma20 <= ma20_prev:
        continue

      # 필터 3: 최근 window일 중 하락일 >= window-1
      if _count_down_days(df, window) < window - 1:
        continue

      # 필터 4: close가 MA20 기준 ±band% 이내 (위로는 band*0.3%까지만)
      pullback_pct = (close - ma20) / ma20 * 100
      if not (-band <= pullback_pct <= band * 0.3):
        continue

      # 필터 5: 고점 대비 하락률 >= drawdown_thresh (예: -10% 이상 빠져야 통과)
      recent_high = float(df["high"].iloc[-20:].max())
      drawdown = (close - recent_high) / recent_high * 100
      if drawdown > drawdown_thresh:
        # drawdown=-5이면 -5 > -10이므로 스킵 (덜 빠진 경우 제외)
        continue

      # 필터 6: 거래량 수축 (최근 window일 평균 < 직전 20일 평균 × vol_ratio)
      vol_window = df["volume"].iloc[-window:]
      vol_prev20 = df["volume"].iloc[-(20 + window):-window]
      if vol_prev20.mean() == 0:
        continue
      vol_decay_score = bool(vol_window.mean() < vol_prev20.mean() * vol_ratio)

      # 필터 7: RSI 과매수 제외
      if rsi > rsi_ob:
        continue

      # 추세 품질: 거래량 연속 감소 여부
      vol_consec_drop = all(
        df["volume"].iloc[-idx] < df["volume"].iloc[-(idx + 1)]
        for idx in range(1, window)
      )

      tp, sl, rr = _calc_exit_prices_coin(close, atr, tp_mult, sl_mult)

      results.append({
        "ticker": ticker,
        "name": ticker,
        "close": close,
        "buy_price": close,
        "take_profit": tp,
        "stop_loss": sl,
        "risk_reward": rr,
        "pullback_pct": round(pullback_pct, 2),
        "pullback_band": band,
        "vol_consec_drop": vol_consec_drop,
        "vol_decay_score": vol_decay_score,
        "volume_24h": float(listing_map.get(ticker, {}).get("volume_24h", 0)),
        "rsi": round(rsi, 1),
        "atr": round(atr, 4),
        "btc_direction": "상승",
      })

    except Exception:
      continue

  if _has_progress:
    try:
      progress.empty()
    except Exception:
      pass

  if not results:
    return pd.DataFrame()

  return _select_best3_coin(pd.DataFrame(results))


def scan_coin_swing() -> pd.DataFrame:
  """스윙 코인 눌림목 스캔 (MA60 기준).

  7가지 필터를 모두 통과한 종목에 가중치 스코어를 적용해 상위 3개 반환.
  BTC 시장 방향이 '상승'이 아니면 빈 DataFrame 반환.
  """
  # BTC 시장 방향 확인
  if get_btc_direction() != "상승":
    return pd.DataFrame()

  cfg = _CFG["strategy"]["swing"]
  ma_short: int = cfg["ma_short"]          # 60
  ma_long: int = cfg["ma_long"]            # 120
  window: int = cfg["pullback_window"]     # 5
  band: float = cfg["pullback_band"]       # 10.0
  vol_ratio: float = cfg["vol_decay_ratio"]  # 0.70
  rsi_ob: float = cfg["rsi_overbought"]    # 75
  drawdown_thresh: float = cfg["drawdown_from_high"]  # -15.0
  tp_mult: float = cfg["tp_mult"]
  sl_mult: float = cfg["sl_mult"]

  # 거래대금 필터 통과 코인 목록 조회
  listing_df = get_coin_listing(min_volume_krw=_CFG["market"]["min_24h_volume_krw"])
  if listing_df.empty:
    return pd.DataFrame()

  # 제외 티커 필터링 (BTC 등)
  exclude: list[str] = _CFG["market"].get("exclude_tickers", [])
  listing_df = listing_df[~listing_df["ticker"].isin(exclude)].reset_index(drop=True)
  if listing_df.empty:
    return pd.DataFrame()

  # volume_24h 빠른 접근용 dict
  listing_map: dict[str, dict[str, float]] = {
    row["ticker"]: {"volume_24h": row["volume_24h"]}
    for _, row in listing_df.iterrows()
  }

  tickers: list[str] = listing_df["ticker"].tolist()
  results: list[dict[str, object]] = []

  # Streamlit progress bar (Streamlit 환경이 아닐 경우 안전 처리)
  try:
    progress = st.progress(0, text="코인 스윙 스캔 중...")
    _has_progress = True
  except Exception:
    _has_progress = False

  for i, ticker in enumerate(tickers):
    try:
      if _has_progress:
        progress.progress(
          (i + 1) / len(tickers),
          text=f"분석 중: {ticker} ({i + 1}/{len(tickers)})",
        )

      df = get_ohlcv_coin(ticker, count=200)
      # MA120 + 여유 확보 최소 130봉 필요
      if df.empty or len(df) < 130:
        continue

      df = add_moving_averages(df)
      df = add_atr(df)
      df = add_rsi(df)
      df = df.dropna(subset=[f"MA{ma_short}", f"MA{ma_long}", "ATR", "RSI"])

      if len(df) < window + 2:
        continue

      close = float(df["close"].iloc[-1])
      ma60 = float(df[f"MA{ma_short}"].iloc[-1])
      ma120 = float(df[f"MA{ma_long}"].iloc[-1])
      ma60_prev = float(df[f"MA{ma_short}"].iloc[-2])
      atr = float(df["ATR"].iloc[-1])
      rsi = float(df["RSI"].iloc[-1])

      # 필터 1: 추세 (close > MA60 > MA120)
      if not (close > ma60 > ma120):
        continue

      # 필터 2: MA60 우상향
      if ma60 <= ma60_prev:
        continue

      # 필터 3: 최근 window일 중 하락일 >= window-1
      if _count_down_days(df, window) < window - 1:
        continue

      # 필터 4: close가 MA60 기준 ±band% 이내 (위로는 band*0.3%까지만)
      pullback_pct = (close - ma60) / ma60 * 100
      if not (-band <= pullback_pct <= band * 0.3):
        continue

      # 필터 5: 고점 대비 하락률 >= drawdown_thresh (예: -15% 이상 빠져야 통과)
      recent_high = float(df["high"].iloc[-20:].max())
      drawdown = (close - recent_high) / recent_high * 100
      if drawdown > drawdown_thresh:
        # drawdown=-5이면 -5 > -15이므로 스킵 (덜 빠진 경우 제외)
        continue

      # 필터 6: 거래량 수축 (최근 window일 평균 < 직전 20일 평균 × vol_ratio)
      vol_window = df["volume"].iloc[-window:]
      vol_prev20 = df["volume"].iloc[-(20 + window):-window]
      if vol_prev20.mean() == 0:
        continue
      vol_decay_score = bool(vol_window.mean() < vol_prev20.mean() * vol_ratio)

      # 필터 7: RSI 과매수 제외
      if rsi > rsi_ob:
        continue

      # 추세 품질: 거래량 연속 감소 여부 (최근 window일 중 마지막 3일 기준)
      vol_consec_drop = all(
        df["volume"].iloc[-idx] < df["volume"].iloc[-(idx + 1)]
        for idx in range(1, window)
      )

      tp, sl, rr = _calc_exit_prices_coin(close, atr, tp_mult, sl_mult)

      results.append({
        "ticker": ticker,
        "name": ticker,
        "close": close,
        "buy_price": close,
        "take_profit": tp,
        "stop_loss": sl,
        "risk_reward": rr,
        "pullback_pct": round(pullback_pct, 2),
        "pullback_band": band,
        "vol_consec_drop": vol_consec_drop,
        "vol_decay_score": vol_decay_score,
        "volume_24h": float(listing_map.get(ticker, {}).get("volume_24h", 0)),
        "rsi": round(rsi, 1),
        "atr": round(atr, 4),
        "btc_direction": "상승",
      })

    except Exception:
      continue

  if _has_progress:
    try:
      progress.empty()
    except Exception:
      pass

  if not results:
    return pd.DataFrame()

  return _select_best3_coin(pd.DataFrame(results))
