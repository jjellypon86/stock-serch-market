from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
import yaml

from utils import add_atr, add_moving_averages, add_rsi, get_ohlcv, get_stock_listing

with open("config.yaml") as _f:
    _cfg = yaml.safe_load(_f)

DAY_TP_MULT = _cfg["scanner"]["day"]["tp_mult"]
DAY_SL_MULT = _cfg["scanner"]["day"]["sl_mult"]
DAY_MAX_HOLD = 5

SWING_TP_MULT = _cfg["scanner"]["swing"]["tp_mult"]
SWING_SL_MULT = _cfg["scanner"]["swing"]["sl_mult"]
SWING_MAX_HOLD = 10

MIN_MARKET_CAP = 30_000_000_000
BACKTEST_SAMPLE_SIZE = 200
SLIPPAGE = 0.001  # 매수 슬리피지 0.1%

PULLBACK_BAND_PCT = _cfg["scanner"]["pullback_band"] * 100  # scanner.py와 동일 기준 (2.1%)


def _get_date_range(end_date: str, days: int) -> str:
    return (datetime.strptime(end_date, "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")


def _simulate_trade(
    df: pd.DataFrame,
    entry_idx: int,
    tp_price: float,
    sl_price: float,
    max_hold: int,
) -> dict | None:
    """신호 발생 다음날 시가 매수(+슬리피지) → scanner.py와 동일한 목표가/손절가(신호일 종가 ± ATR×배수)로 청산 시뮬레이션"""
    if entry_idx + 1 >= len(df) or sl_price <= 0 or sl_price >= tp_price:
        return None

    raw_price = df["시가"].iloc[entry_idx + 1]
    if raw_price <= 0:
        return None

    entry_price = raw_price * (1 + SLIPPAGE)  # 슬리피지 반영
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


def _prepare_day_df(df: pd.DataFrame) -> pd.DataFrame:
    df = add_moving_averages(df)
    df = add_atr(df)
    df = add_rsi(df)
    return df.dropna(subset=["MA20", "MA60", "ATR", "RSI"]).reset_index(drop=True)


def _prepare_swing_df(df: pd.DataFrame) -> pd.DataFrame:
    df = add_moving_averages(df)
    df = add_atr(df)
    df = add_rsi(df)
    return df.dropna(subset=["MA20", "MA60", "MA120", "ATR", "RSI"]).reset_index(drop=True)


def _find_day_signals(df: pd.DataFrame) -> list[dict]:
    """
    단기 눌림목 신호 — scanner.py scan_day_trading()과 동일 조건.
    df는 _prepare_day_df()로 지표 계산 + dropna + reset_index 완료된 상태여야 함.
    기관/외국인 수급 필터는 과거 시점 데이터를 제공하는 소스가 없어 제외 (README 참고).
    """
    signals = []
    for i in range(22, len(df)):
        close = df["종가"].iloc[i]
        ma60 = df["MA60"].iloc[i]

        # ① 중기 상승 추세 (MA60 위 유지, MA20 단기 이탈 허용)
        if not (close > ma60 and ma60 > df["MA60"].iloc[i - 1]):
            continue
        # ② 최근 3일 중 하락일 >= 2
        if _count_down_days(df, i, window=3) < 2:
            continue

        # ③ MA20 ±band% 이내
        ma20 = df["MA20"].iloc[i]
        pullback_pct = (close - ma20) / ma20 * 100
        if not (-PULLBACK_BAND_PCT <= pullback_pct <= 1.0):
            continue

        # MA20 연속 3일 우상향
        if not (df["MA20"].iloc[i] > df["MA20"].iloc[i - 1] > df["MA20"].iloc[i - 2]):
            continue

        # 최근 20일 고점 대비 -5% 이상 눌린 경우만 허용
        recent_high = df["고가"].iloc[max(0, i - 19):i + 1].max()
        if (close - recent_high) / recent_high * 100 > -5.0:
            continue

        # ④ 거래량 감소 (눌림 3일 vs 직전 20일 비교 — 구간 분리)
        vol_3d = df["거래량"].iloc[i - 2:i + 1].mean()
        vol_prev = df["거래량"].iloc[i - 22:i - 2].mean()
        if vol_prev == 0 or vol_3d >= vol_prev * 0.7:
            continue

        # RSI 하드 필터: 과매수 구간 제외
        if df["RSI"].iloc[i] > 70:
            continue

        atr = df["ATR"].iloc[i]
        signals.append({
            "idx": i,
            "tp_price": close + atr * DAY_TP_MULT,
            "sl_price": close - atr * DAY_SL_MULT,
        })
    return signals


def _find_swing_signals(df: pd.DataFrame) -> list[dict]:
    """
    스윙 눌림목 신호 — scanner.py scan_swing()과 동일 조건.
    df는 _prepare_swing_df()로 지표 계산 + dropna + reset_index 완료된 상태여야 함.
    기관/외국인 수급 필터는 과거 시점 데이터를 제공하는 소스가 없어 제외.
    """
    signals = []
    for i in range(24, len(df)):
        close = df["종가"].iloc[i]
        ma120 = df["MA120"].iloc[i]

        # ① 장기 상승 추세 (MA120 위 유지, MA60 단기 이탈 허용)
        if not (close > ma120 and ma120 > df["MA120"].iloc[i - 1]):
            continue
        # ② 최근 5일 중 하락일 >= 3
        if _count_down_days(df, i, window=5) < 3:
            continue

        # ③ MA60 ±band% 이내
        ma60 = df["MA60"].iloc[i]
        pullback_pct = (close - ma60) / ma60 * 100
        if not (-PULLBACK_BAND_PCT <= pullback_pct <= 1.0):
            continue

        # MA20 연속 3일 우상향 (단기 지지선 + 추세 역전 조기 차단)
        if not (df["MA20"].iloc[i] > df["MA20"].iloc[i - 1] > df["MA20"].iloc[i - 2]):
            continue

        # ④ 거래량 감소 (눌림 5일 vs 직전 20일 비교 — 구간 분리)
        vol_5d = df["거래량"].iloc[i - 4:i + 1].mean()
        vol_prev = df["거래량"].iloc[i - 24:i - 4].mean()
        if vol_prev == 0 or vol_5d >= vol_prev * 0.7:
            continue

        atr = df["ATR"].iloc[i]
        signals.append({
            "idx": i,
            "tp_price": close + atr * SWING_TP_MULT,
            "sl_price": close - atr * SWING_SL_MULT,
        })
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
    전략 백테스트 — scanner.py 실시간 스캔(v2)과 동일한 필터·목표가 로직으로 신호 탐지
    - 기관/외국인 수급 필터는 제외: 데이터 소스(네이버 증권 API)가 최근 수일치만 제공해
      과거 시점 재현이 불가능함. 수급 필터는 WIN 기대값에 양의 가중치(43%)가 걸려 있어
      실제 라이브 전략의 승률은 이 백테스트 결과 이상일 가능성이 높음
    - 생존 편향 제거: 시총 상위 N 대신 시총 300억↑ 전체에서 랜덤 샘플링
    - 슬리피지 0.1% 반영
    - 목표가/손절가: scanner.py와 동일하게 신호일 종가 ± ATR × tp/sl_mult
    - MDD: 복리 누적 수익률 기반
    """
    end_date = datetime.now().strftime("%Y%m%d")
    # MA120 확보를 위해 150일 추가
    start_date = _get_date_range(end_date, days=months * 30 + 150)

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

        df_raw = get_ohlcv(ticker, start_date, end_date)
        if len(df_raw) < min_rows:
            continue

        df = _prepare_day_df(df_raw) if strategy == "day" else _prepare_swing_df(df_raw)
        if len(df) < 10:
            continue

        signals = _find_day_signals(df) if strategy == "day" else _find_swing_signals(df)

        for sig in signals:
            trade = _simulate_trade(df, sig["idx"], sig["tp_price"], sig["sl_price"], max_hold)
            if trade:
                trade["ticker"] = ticker
                all_trades.append(trade)

    progress.empty()
    return _aggregate_results(all_trades)
