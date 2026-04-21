from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from utils import (
    add_atr,
    add_moving_averages,
    add_rsi,
    get_ohlcv,
    get_stock_listing,
    get_ticker_name,
)

MIN_MARKET_CAP = 30_000_000_000   # 시가총액 300억 하한선
MIN_TRADE_AMOUNT = 1_000_000_000  # 거래대금 10억 하한선

DAY_TP_MULT = 2.0
DAY_SL_MULT = 1.0
SWING_TP_MULT = 3.0
SWING_SL_MULT = 1.5


def calc_net_profit(buy: float, sell: float, qty: int) -> float:
    """수수료(0.015%) + 거래세(0.2%) 반영 순수익"""
    return sell * qty * (1 - 0.00015 - 0.002) - buy * qty * (1 + 0.00015)


def _get_date_range(end_date: str, days: int = 60) -> str:
    return (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")


def _get_prev_date(date: str) -> str:
    dt = datetime.strptime(date, "%Y%m%d") - timedelta(days=1)
    while dt.weekday() >= 5:
        dt -= timedelta(days=1)
    return dt.strftime("%Y%m%d")


def _calc_exit_prices(close: float, atr: float, tp_mult: float, sl_mult: float) -> tuple[int, int, float]:
    tp = close + atr * tp_mult
    sl = close - atr * sl_mult
    rr = round((tp - close) / (close - sl), 2) if close > sl else 0.0
    return int(tp), int(sl), rr


def scan_day_trading(date: str, market: str = "KOSPI") -> pd.DataFrame:
    """
    단기 종목 스캔
    1단계: 오늘 전체 종목 스냅샷 → 시총/거래대금/양봉 필터
    2단계: 필터된 종목에 개별 OHLCV → 거래량 비율 + RSI 30 상향돌파
    """
    df_listing = get_stock_listing(market)
    if df_listing.empty:
        return pd.DataFrame()

    # 1단계: 기본 필터 (시총/거래대금/양봉)
    candidates = df_listing[
        (df_listing["market_cap"] >= MIN_MARKET_CAP) &
        (df_listing["거래대금"] >= MIN_TRADE_AMOUNT) &
        (df_listing["종가"] > df_listing["시가"])
    ].copy()

    if candidates.empty:
        return pd.DataFrame()

    results: list[dict] = []
    progress = st.progress(0, text="종목 분석 중...")
    tickers = candidates.index.tolist()

    for i, ticker in enumerate(tickers):
        progress.progress((i + 1) / len(tickers), text=f"분석 중: {ticker}")

        start = _get_date_range(date, days=60)
        df_ohlcv = get_ohlcv(ticker, start, date)

        if len(df_ohlcv) < 20:
            continue

        # 거래량 비율: 당일 vs 전일
        if len(df_ohlcv) < 2:
            continue
        vol_today = df_ohlcv["거래량"].iloc[-1]
        vol_prev = df_ohlcv["거래량"].iloc[-2]
        if vol_prev == 0:
            continue
        volume_ratio = vol_today / vol_prev
        if volume_ratio < 2.0:
            continue

        df_ohlcv = add_rsi(df_ohlcv)
        df_ohlcv = add_atr(df_ohlcv)
        df_ohlcv = df_ohlcv.dropna(subset=["RSI", "ATR"])

        if len(df_ohlcv) < 2:
            continue

        rsi_today = df_ohlcv["RSI"].iloc[-1]
        rsi_prev = df_ohlcv["RSI"].iloc[-2]

        # RSI 30 상향 돌파
        if not (rsi_prev < 30 and rsi_today > 30):
            continue

        close = int(candidates.loc[ticker, "종가"])
        atr = df_ohlcv["ATR"].iloc[-1]
        tp, sl, rr = _calc_exit_prices(close, atr, DAY_TP_MULT, DAY_SL_MULT)
        net_profit_pct = round(calc_net_profit(close, tp, 1) / close * 100, 2)

        results.append({
            "ticker": ticker,
            "name": candidates.loc[ticker, "name"],
            "close": close,
            "volume_ratio": round(volume_ratio, 2),
            "rsi_prev": round(rsi_prev, 1),
            "rsi_today": round(rsi_today, 1),
            "take_profit": tp,
            "stop_loss": sl,
            "risk_reward": rr,
            "net_profit_pct": net_profit_pct,
        })

    progress.empty()

    if not results:
        return pd.DataFrame()

    return pd.DataFrame(results).sort_values("volume_ratio", ascending=False).reset_index(drop=True)


def scan_swing(end_date: str, market: str = "KOSPI") -> pd.DataFrame:
    """
    스윙 종목 스캔
    조건: MA5/MA20 골든크로스 + 거래대금 10억↑ + 시총 300억↑
    수급 데이터 없이 기술적 조건만으로 스캔
    """
    df_listing = get_stock_listing(market)
    if df_listing.empty:
        return pd.DataFrame()

    # 시총/거래대금 필터
    candidates = df_listing[
        (df_listing["market_cap"] >= MIN_MARKET_CAP) &
        (df_listing["거래대금"] >= MIN_TRADE_AMOUNT)
    ].copy()

    tickers = candidates.index.tolist()
    start = _get_date_range(end_date, days=60)

    results: list[dict] = []
    progress = st.progress(0, text="스윙 스캔 중...")

    for i, ticker in enumerate(tickers):
        progress.progress((i + 1) / len(tickers), text=f"분석 중: {ticker}")

        df_ohlcv = get_ohlcv(ticker, start, end_date)
        if len(df_ohlcv) < 25:
            continue

        df_ohlcv = add_moving_averages(df_ohlcv)
        df_ohlcv = add_atr(df_ohlcv)
        df_ohlcv = df_ohlcv.dropna(subset=["MA5", "MA20", "ATR"])

        if len(df_ohlcv) < 2:
            continue

        ma5_today = df_ohlcv["MA5"].iloc[-1]
        ma20_today = df_ohlcv["MA20"].iloc[-1]
        ma5_prev = df_ohlcv["MA5"].iloc[-2]
        ma20_prev = df_ohlcv["MA20"].iloc[-2]

        # 골든크로스
        if not (ma5_prev < ma20_prev and ma5_today > ma20_today):
            continue

        close = int(df_ohlcv["종가"].iloc[-1])
        atr = df_ohlcv["ATR"].iloc[-1]
        tp, sl, rr = _calc_exit_prices(close, atr, SWING_TP_MULT, SWING_SL_MULT)

        # 이격도: MA5와 MA20의 간격 (%)
        ma_gap_pct = round((ma5_today - ma20_today) / ma20_today * 100, 2)

        results.append({
            "ticker": ticker,
            "name": candidates.loc[ticker, "name"],
            "close": close,
            "ma5": int(ma5_today),
            "ma20": int(ma20_today),
            "ma_gap_pct": ma_gap_pct,
            "take_profit": tp,
            "stop_loss": sl,
            "risk_reward": rr,
        })

    progress.empty()

    if not results:
        return pd.DataFrame()

    return pd.DataFrame(results).sort_values("ma_gap_pct", ascending=True).reset_index(drop=True)
