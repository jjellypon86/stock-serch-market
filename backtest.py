from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from utils import add_moving_averages, get_ohlcv, get_stock_listing

DAY_TP_PCT = 0.05
DAY_SL_PCT = 0.02
DAY_MAX_HOLD = 5

SWING_TP_PCT = 0.07
SWING_SL_PCT = 0.03
SWING_MAX_HOLD = 10

MIN_MARKET_CAP = 30_000_000_000
BACKTEST_SAMPLE_SIZE = 200
SLIPPAGE = 0.001  # 매수 슬리피지 0.1%

PULLBACK_BAND = 0.03  # MA 기준 ±3%


def _get_date_range(end_date: str, days: int) -> str:
    return (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")


def _simulate_trade(
    df: pd.DataFrame,
    entry_idx: int,
    tp_pct: float,
    sl_pct: float,
    max_hold: int,
) -> dict | None:
    """신호 발생 다음날 시가 매수 → 슬리피지 반영 → 익절/손절/기간만료 시뮬레이션"""
    if entry_idx + 1 >= len(df):
        return None

    raw_price = df["시가"].iloc[entry_idx + 1]
    if raw_price <= 0:
        return None

    entry_price = raw_price * (1 + SLIPPAGE)  # 슬리피지 반영
    tp_price = entry_price * (1 + tp_pct)
    sl_price = entry_price * (1 - sl_pct)
    end_idx = min(entry_idx + 1 + max_hold, len(df))

    for i in range(entry_idx + 1, end_idx):
        high = df["고가"].iloc[i]
        low = df["저가"].iloc[i]

        # 동시 도달 시 손절 우선 (보수적)
        if low <= sl_price and high >= tp_price:
            return {"result": "손절", "profit_pct": round((sl_price / entry_price - 1) * 100, 2), "hold_days": i - entry_idx}
        if high >= tp_price:
            return {"result": "익절", "profit_pct": round((tp_price / entry_price - 1) * 100, 2), "hold_days": i - entry_idx}
        if low <= sl_price:
            return {"result": "손절", "profit_pct": round((sl_price / entry_price - 1) * 100, 2), "hold_days": i - entry_idx}

    exit_price = df["종가"].iloc[min(entry_idx + max_hold, len(df) - 1)]
    return {"result": "기간만료", "profit_pct": round((exit_price / entry_price - 1) * 100, 2), "hold_days": max_hold}


def _count_down_days(df: pd.DataFrame, end_i: int, window: int) -> int:
    """end_i 기준 최근 window일 중 하락일 수"""
    count = 0
    for j in range(end_i - window + 1, end_i + 1):
        if j > 0 and df["종가"].iloc[j] < df["종가"].iloc[j - 1]:
            count += 1
    return count


def _find_day_signals(df: pd.DataFrame) -> list[int]:
    """단기 눌림목 신호 인덱스 (MA20 기준)"""
    df = add_moving_averages(df).dropna(subset=["MA20", "MA60"])
    signals = []
    for i in range(4, len(df)):
        close = df["종가"].iloc[i]
        ma20 = df["MA20"].iloc[i]
        ma60 = df["MA60"].iloc[i]

        if not (close > ma20 > ma60):
            continue
        if _count_down_days(df, i, window=3) < 2:
            continue
        pullback_pct = abs((close - ma20) / ma20)
        if pullback_pct > PULLBACK_BAND:
            continue
        vol_3d = df["거래량"].iloc[i - 2:i + 1].mean()
        vol_20d = df["거래량"].iloc[max(0, i - 19):i + 1].mean()
        if vol_20d == 0 or vol_3d >= vol_20d * 0.7:
            continue

        signals.append(i)
    return signals


def _find_swing_signals(df: pd.DataFrame) -> list[int]:
    """스윙 눌림목 신호 인덱스 (MA60 기준)"""
    df = add_moving_averages(df).dropna(subset=["MA60", "MA120"])
    signals = []
    for i in range(6, len(df)):
        close = df["종가"].iloc[i]
        ma60 = df["MA60"].iloc[i]
        ma120 = df["MA120"].iloc[i]

        if not (close > ma60 > ma120):
            continue
        if _count_down_days(df, i, window=5) < 3:
            continue
        pullback_pct = abs((close - ma60) / ma60)
        if pullback_pct > PULLBACK_BAND:
            continue
        vol_5d = df["거래량"].iloc[i - 4:i + 1].mean()
        vol_20d = df["거래량"].iloc[max(0, i - 19):i + 1].mean()
        if vol_20d == 0 or vol_5d >= vol_20d * 0.7:
            continue

        signals.append(i)
    return signals


def _aggregate_results(trades: list[dict]) -> dict:
    """트레이드 결과 집계 — 복리 기반 MDD 계산"""
    if not trades:
        return {}

    df = pd.DataFrame(trades)
    wins = df[df["result"] == "익절"]
    losses = df[df["result"].isin(["손절", "기간만료"])]

    win_rate = len(wins) / len(df) * 100
    avg_profit = wins["profit_pct"].mean() if not wins.empty else 0.0
    avg_loss = losses["profit_pct"].mean() if not losses.empty else 0.0
    expectancy = (win_rate / 100 * avg_profit) + ((1 - win_rate / 100) * avg_loss)

    # 복리 누적 수익률 기반 MDD
    df["cumulative_return"] = (1 + df["profit_pct"] / 100).cumprod()
    peak = df["cumulative_return"].cummax()
    mdd = ((df["cumulative_return"] - peak) / peak).min() * 100

    return {
        "총 거래수": len(df),
        "익절": len(wins),
        "손절+만료": len(losses),
        "승률(%)": round(win_rate, 1),
        "평균 수익(%)": round(avg_profit, 2),
        "평균 손실(%)": round(avg_loss, 2),
        "기대값(%)": round(expectancy, 2),
        "MDD(%)": round(mdd, 2),
        "trades": trades,
    }


def run_backtest(strategy: str = "day", months: int = 3, market: str = "KOSPI") -> dict:
    """
    전략 백테스트
    - 생존 편향 제거: 시총 상위 N 대신 시총 300억↑ 전체에서 랜덤 샘플링
    - 슬리피지 0.1% 반영
    - MDD: 복리 누적 수익률 기반
    """
    end_date = datetime.now().strftime("%Y%m%d")
    # MA120 확보를 위해 150일 추가
    start_date = _get_date_range(end_date, days=months * 30 + 150)

    tp_pct = DAY_TP_PCT if strategy == "day" else SWING_TP_PCT
    sl_pct = DAY_SL_PCT if strategy == "day" else SWING_SL_PCT
    max_hold = DAY_MAX_HOLD if strategy == "day" else SWING_MAX_HOLD
    min_rows = 65 if strategy == "day" else 125

    df_listing = get_stock_listing(market)
    if df_listing.empty:
        st.warning("종목 데이터를 가져올 수 없습니다.")
        return {}

    # 생존 편향 제거: 랜덤 샘플링
    universe = df_listing[df_listing["market_cap"] >= MIN_MARKET_CAP]
    if len(universe) > BACKTEST_SAMPLE_SIZE:
        universe = universe.sample(BACKTEST_SAMPLE_SIZE, random_state=42)
    tickers = universe.index.tolist()

    all_trades: list[dict] = []
    progress = st.progress(0, text="백테스트 진행 중...")

    for i, ticker in enumerate(tickers):
        progress.progress((i + 1) / len(tickers), text=f"백테스트: {ticker} ({i+1}/{len(tickers)})")

        df_ohlcv = get_ohlcv(ticker, start_date, end_date)
        if len(df_ohlcv) < min_rows:
            continue

        signals = _find_day_signals(df_ohlcv) if strategy == "day" else _find_swing_signals(df_ohlcv)

        for sig_idx in signals:
            trade = _simulate_trade(df_ohlcv, sig_idx, tp_pct, sl_pct, max_hold)
            if trade:
                trade["ticker"] = ticker
                all_trades.append(trade)

    progress.empty()
    return _aggregate_results(all_trades)
