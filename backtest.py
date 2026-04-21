from datetime import datetime, timedelta

import FinanceDataReader as fdr
import pandas as pd
import streamlit as st

from utils import add_moving_averages, add_rsi, get_ohlcv, get_stock_listing

DAY_TP_PCT = 0.05
DAY_SL_PCT = 0.02
DAY_MAX_HOLD = 5

SWING_TP_PCT = 0.07
SWING_SL_PCT = 0.03
SWING_MAX_HOLD = 10

MIN_MARKET_CAP = 30_000_000_000
BACKTEST_SAMPLE_SIZE = 100


def _get_date_range(end_date: str, days: int) -> str:
    return (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")


def _simulate_trade(
    df: pd.DataFrame,
    entry_idx: int,
    tp_pct: float,
    sl_pct: float,
    max_hold: int,
) -> dict | None:
    """신호 발생 다음날 시가 매수 → 익절/손절/기간만료 시뮬레이션"""
    if entry_idx + 1 >= len(df):
        return None

    entry_price = df["시가"].iloc[entry_idx + 1]
    if entry_price <= 0:
        return None

    tp_price = entry_price * (1 + tp_pct)
    sl_price = entry_price * (1 - sl_pct)
    end_idx = min(entry_idx + 1 + max_hold, len(df))

    for i in range(entry_idx + 1, end_idx):
        high = df["고가"].iloc[i]
        low = df["저가"].iloc[i]

        # 같은 날 익절/손절 동시 도달 → 손절 우선 (보수적)
        if low <= sl_price and high >= tp_price:
            return {"result": "손절", "profit_pct": round((sl_price / entry_price - 1) * 100, 2), "hold_days": i - entry_idx}
        if high >= tp_price:
            return {"result": "익절", "profit_pct": round((tp_price / entry_price - 1) * 100, 2), "hold_days": i - entry_idx}
        if low <= sl_price:
            return {"result": "손절", "profit_pct": round((sl_price / entry_price - 1) * 100, 2), "hold_days": i - entry_idx}

    exit_price = df["종가"].iloc[min(entry_idx + max_hold, len(df) - 1)]
    return {"result": "기간만료", "profit_pct": round((exit_price / entry_price - 1) * 100, 2), "hold_days": max_hold}


def _find_day_signals(df: pd.DataFrame) -> list[int]:
    """RSI 30 상향 돌파 + 양봉 신호 인덱스"""
    df = add_rsi(df).dropna(subset=["RSI"])
    return [
        i for i in range(1, len(df))
        if df["RSI"].iloc[i - 1] < 30
        and df["RSI"].iloc[i] > 30
        and df["종가"].iloc[i] > df["시가"].iloc[i]
    ]


def _find_swing_signals(df: pd.DataFrame) -> list[int]:
    """MA5/MA20 골든크로스 신호 인덱스"""
    df = add_moving_averages(df).dropna(subset=["MA5", "MA20"])
    return [
        i for i in range(1, len(df))
        if df["MA5"].iloc[i - 1] < df["MA20"].iloc[i - 1]
        and df["MA5"].iloc[i] > df["MA20"].iloc[i]
    ]


def _aggregate_results(trades: list[dict]) -> dict:
    """트레이드 결과 집계"""
    if not trades:
        return {}

    df = pd.DataFrame(trades)
    wins = df[df["result"] == "익절"]
    losses = df[df["result"].isin(["손절", "기간만료"])]

    win_rate = len(wins) / len(df) * 100
    avg_profit = wins["profit_pct"].mean() if not wins.empty else 0.0
    avg_loss = losses["profit_pct"].mean() if not losses.empty else 0.0
    expectancy = (win_rate / 100 * avg_profit) + ((1 - win_rate / 100) * avg_loss)

    cumulative = df["profit_pct"].cumsum()
    mdd = (cumulative - cumulative.cummax()).min()

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
    """전략 백테스트 (시총 상위 N개 종목 대상)"""
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = _get_date_range(end_date, days=months * 30)

    tp_pct = DAY_TP_PCT if strategy == "day" else SWING_TP_PCT
    sl_pct = DAY_SL_PCT if strategy == "day" else SWING_SL_PCT
    max_hold = DAY_MAX_HOLD if strategy == "day" else SWING_MAX_HOLD

    df_listing = get_stock_listing(market)
    if df_listing.empty:
        st.warning("종목 데이터를 가져올 수 없습니다.")
        return {}

    top_tickers = (
        df_listing[df_listing["market_cap"] >= MIN_MARKET_CAP]
        .nlargest(BACKTEST_SAMPLE_SIZE, "market_cap")
        .index.tolist()
    )

    all_trades: list[dict] = []
    progress = st.progress(0, text="백테스트 진행 중...")

    for i, ticker in enumerate(top_tickers):
        progress.progress((i + 1) / len(top_tickers), text=f"백테스트: {ticker} ({i+1}/{len(top_tickers)})")

        df_ohlcv = get_ohlcv(ticker, start_date, end_date)
        if len(df_ohlcv) < 30:
            continue

        signals = _find_day_signals(df_ohlcv) if strategy == "day" else _find_swing_signals(df_ohlcv)

        for sig_idx in signals:
            trade = _simulate_trade(df_ohlcv, sig_idx, tp_pct, sl_pct, max_hold)
            if trade:
                trade["ticker"] = ticker
                all_trades.append(trade)

    progress.empty()
    return _aggregate_results(all_trades)
