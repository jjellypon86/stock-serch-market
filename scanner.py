from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from utils import (
    add_atr,
    add_moving_averages,
    get_investor_flow,
    get_ohlcv,
    get_stock_listing,
)

MIN_MARKET_CAP   = 30_000_000_000   # 시가총액 300억 하한선
DAY_MIN_TRADE    = 5_000_000_000   # 단기 거래대금 50억↑
SWING_MIN_TRADE  = 10_000_000_000  # 스윙 거래대금 100억↑

DAY_TP_MULT = 2.0
DAY_SL_MULT = 1.0
SWING_TP_MULT = 3.0
SWING_SL_MULT = 1.5

# 눌림목 허용 범위: MA 기준 ±3%
PULLBACK_BAND = 0.03


def _select_best2(df: pd.DataFrame) -> pd.DataFrame:
    """수급(40%)·손익비(35%)·눌림률(25%) 가중 점수로 상위 2개 반환"""
    if df.empty:
        return df
    df = df.copy()
    df["_score"] = (
        (df["inst_days"] + df["foreign_days"]) / 6 * 40
        + df["risk_reward"].clip(upper=3.0) / 3.0 * 35
        + (1 - df["pullback_pct"].abs() / 5.0).clip(lower=0) * 25
    )
    return df.nlargest(2, "_score").drop(columns="_score").reset_index(drop=True)


def calc_net_profit(buy: float, sell: float, qty: int) -> float:
    """수수료(0.015%) + 거래세(0.2%) 반영 순수익"""
    return sell * qty * (1 - 0.00015 - 0.002) - buy * qty * (1 + 0.00015)


def _get_date_range(end_date: str, days: int = 60) -> str:
    return (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")


def _calc_exit_prices(close: float, atr: float, tp_mult: float, sl_mult: float) -> tuple[int, int, float]:
    tp = close + atr * tp_mult
    sl = close - atr * sl_mult
    rr = round((tp - close) / (close - sl), 2) if close > sl else 0.0
    return int(tp), int(sl), rr


def _count_down_days(df: pd.DataFrame, window: int) -> int:
    """최근 window일 중 하락일(종가 < 전일 종가) 수"""
    closes = df["종가"].iloc[-(window + 1):]
    return sum(
        1 for i in range(1, len(closes))
        if closes.iloc[i] < closes.iloc[i - 1]
    )


def scan_day_trading(date: str, market: str = "KOSPI") -> pd.DataFrame:
    """
    단기 눌림목 스캔 (MA20 기준)
    ① 종가 > MA20 > MA60 (단기 상승 추세)
    ② 최근 3일 중 하락일 >= 2 (눌림 발생)
    ③ 종가가 MA20 ±3% 이내 (지지선 근처)
    ④ 3일 평균 거래량 < 20일 평균 × 0.7 (매도 압력 약화)
    ⑤ 기관 OR 외국인 최근 3일 중 순매수 >= 2일
    """
    df_listing = get_stock_listing(market)
    if df_listing.empty:
        return pd.DataFrame()

    candidates = df_listing[
        (df_listing["market_cap"] >= MIN_MARKET_CAP) &
        (df_listing["거래대금"] >= DAY_MIN_TRADE)
    ].copy()

    if candidates.empty:
        return pd.DataFrame()

    results: list[dict] = []
    progress = st.progress(0, text="단기 눌림목 스캔 중...")
    tickers = candidates.index.tolist()
    start = _get_date_range(date, days=150)  # MA60 확보

    for i, ticker in enumerate(tickers):
        progress.progress((i + 1) / len(tickers), text=f"분석 중: {ticker} ({i+1}/{len(tickers)})")

        df = get_ohlcv(ticker, start, date)
        if len(df) < 65:
            continue

        df = add_moving_averages(df)
        df = add_atr(df)
        df = df.dropna(subset=["MA20", "MA60", "ATR"])

        if len(df) < 5:
            continue

        close = df["종가"].iloc[-1]
        ma20 = df["MA20"].iloc[-1]
        ma60 = df["MA60"].iloc[-1]

        # ① 중기 상승 추세 (MA60 위 유지, MA20 단기 이탈 허용)
        if not (close > ma60 and ma60 > df["MA60"].iloc[-2]):
            continue

        # ② 최근 3일 중 하락일 >= 2
        if len(df) < 4:
            continue
        if _count_down_days(df, window=3) < 2:
            continue

        # ③ MA20 -3~+1% 이내 (MA20 살짝 아래까지 허용)
        pullback_pct = (close - ma20) / ma20 * 100
        if not (-3.0 <= pullback_pct <= 1.0):
            continue

        # ④ 거래량 감소 (눌림 3일 vs 직전 20일 비교 — 구간 분리)
        if len(df) < 26:
            continue
        vol_3d   = df["거래량"].iloc[-3:].mean()
        vol_prev = df["거래량"].iloc[-23:-3].mean()
        if vol_prev == 0 or vol_3d >= vol_prev * 0.7:
            continue

        # ⑤ 수급 필터
        flow = get_investor_flow(ticker)
        inst_days = 0
        foreign_days = 0
        if flow is not None:
            inst_days = flow["기관_순매수일"]
            foreign_days = flow["외국인_순매수일"]
            if inst_days < 2 and foreign_days < 2:
                continue

        atr = df["ATR"].iloc[-1]
        tp, sl, rr = _calc_exit_prices(close, atr, DAY_TP_MULT, DAY_SL_MULT)
        net_profit_pct = round(calc_net_profit(close, tp, 1) / close * 100, 2)

        results.append({
            "ticker": ticker,
            "name": candidates.loc[ticker, "name"],
            "buy_price": int(close),
            "close": int(close),
            "pullback_pct": round(pullback_pct, 2),
            "vol_ratio": round(vol_3d / vol_prev, 2),
            "inst_days": inst_days,
            "foreign_days": foreign_days,
            "take_profit": tp,
            "stop_loss": sl,
            "risk_reward": rr,
            "net_profit_pct": net_profit_pct,
        })

    progress.empty()

    if not results:
        return pd.DataFrame()

    return _select_best2(pd.DataFrame(results))


def scan_swing(end_date: str, market: str = "KOSPI") -> pd.DataFrame:
    """
    스윙 눌림목 스캔 (MA60 기준)
    ① 종가 > MA60 > MA120 (중기 상승 추세)
    ② 최근 5일 중 하락일 >= 3 (눌림 발생)
    ③ 종가가 MA60 ±3% 이내 (지지선 근처)
    ④ 5일 평균 거래량 < 20일 평균 × 0.7 (매도 압력 약화)
    ⑤ 기관 OR 외국인 최근 3일 중 순매수 >= 2일
    """
    df_listing = get_stock_listing(market)
    if df_listing.empty:
        return pd.DataFrame()

    candidates = df_listing[
        (df_listing["market_cap"] >= MIN_MARKET_CAP) &
        (df_listing["거래대금"] >= SWING_MIN_TRADE)
    ].copy()

    if candidates.empty:
        return pd.DataFrame()

    results: list[dict] = []
    progress = st.progress(0, text="스윙 눌림목 스캔 중...")
    tickers = candidates.index.tolist()
    start = _get_date_range(end_date, days=250)  # MA120 확보

    for i, ticker in enumerate(tickers):
        progress.progress((i + 1) / len(tickers), text=f"분석 중: {ticker} ({i+1}/{len(tickers)})")

        df = get_ohlcv(ticker, start, end_date)
        if len(df) < 125:
            continue

        df = add_moving_averages(df)
        df = add_atr(df)
        df = df.dropna(subset=["MA60", "MA120", "ATR"])

        if len(df) < 7:
            continue

        close = df["종가"].iloc[-1]
        ma60 = df["MA60"].iloc[-1]
        ma120 = df["MA120"].iloc[-1]

        # ① 장기 상승 추세 (MA120 위 유지, MA60 단기 이탈 허용)
        if not (close > ma120 and ma120 > df["MA120"].iloc[-2]):
            continue

        # ② 최근 5일 중 하락일 >= 3
        if _count_down_days(df, window=5) < 3:
            continue

        # ③ MA60 -3~+1% 이내 (MA60 살짝 아래까지 허용)
        pullback_pct = (close - ma60) / ma60 * 100
        if not (-3.0 <= pullback_pct <= 1.0):
            continue

        # ④ 거래량 감소 (눌림 5일 vs 직전 20일 비교 — 구간 분리)
        if len(df) < 28:
            continue
        vol_5d   = df["거래량"].iloc[-5:].mean()
        vol_prev = df["거래량"].iloc[-25:-5].mean()
        if vol_prev == 0 or vol_5d >= vol_prev * 0.7:
            continue

        # ⑤ 수급 필터
        flow = get_investor_flow(ticker)
        inst_days = 0
        foreign_days = 0
        if flow is not None:
            inst_days = flow["기관_순매수일"]
            foreign_days = flow["외국인_순매수일"]
            if inst_days < 2 and foreign_days < 2:
                continue

        atr = df["ATR"].iloc[-1]
        tp, sl, rr = _calc_exit_prices(close, atr, SWING_TP_MULT, SWING_SL_MULT)

        results.append({
            "ticker": ticker,
            "name": candidates.loc[ticker, "name"],
            "buy_price": int(close),
            "close": int(close),
            "pullback_pct": round(pullback_pct, 2),
            "vol_ratio": round(vol_5d / vol_prev, 2),
            "inst_days": inst_days,
            "foreign_days": foreign_days,
            "ma60": int(ma60),
            "ma120": int(ma120),
            "take_profit": tp,
            "stop_loss": sl,
            "risk_reward": rr,
        })

    progress.empty()

    if not results:
        return pd.DataFrame()

    return _select_best2(pd.DataFrame(results))
