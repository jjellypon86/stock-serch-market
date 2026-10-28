from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import streamlit as st
import yaml

from utils import add_atr, add_moving_averages, add_rsi, get_ohlcv, get_stock_listing

with open("config.yaml") as _f:
    _cfg = yaml.safe_load(_f)

DAY_TP_MULT = _cfg["scanner"]["day"]["tp_mult"]
DAY_SL_MULT = _cfg["scanner"]["day"]["sl_mult"]
DAY_MAX_LOSS_PCT = _cfg["scanner"]["day"]["max_loss_pct"]
# backtest.ignore_profit_cap이 true면 익절 상한을 무시(v3 규칙으로 백테스트) — 실매매 스캔에는 영향 없음
DAY_MAX_PROFIT_PCT = (
    None
    if _cfg["backtest"].get("ignore_profit_cap", False)
    else _cfg["scanner"]["day"].get("max_profit_pct")
)
DAY_DRAWDOWN_MIN = _cfg["scanner"]["day"]["drawdown_min"]
DAY_DRAWDOWN_MAX = _cfg["scanner"]["day"]["drawdown_max"]
DAY_MAX_HOLD = 5

SWING_TP_MULT = _cfg["scanner"]["swing"]["tp_mult"]
SWING_SL_MULT = _cfg["scanner"]["swing"]["sl_mult"]
SWING_MAX_LOSS_PCT = _cfg["scanner"]["swing"]["max_loss_pct"]
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
        down_days = _count_down_days(df, i, window=3)
        if down_days < 2:
            continue

        # ③ MA20 ±band% 이내
        ma20 = df["MA20"].iloc[i]
        pullback_pct = (close - ma20) / ma20 * 100
        if not (-PULLBACK_BAND_PCT <= pullback_pct <= 1.0):
            continue

        # MA20 연속 3일 우상향
        if not (df["MA20"].iloc[i] > df["MA20"].iloc[i - 1] > df["MA20"].iloc[i - 2]):
            continue

        # 최근 20일 고점 대비 하락률 밴드 (scanner.py와 동일, v3)
        recent_high = df["고가"].iloc[max(0, i - 19):i + 1].max()
        drawdown_from_high = (close - recent_high) / recent_high * 100
        if not (DAY_DRAWDOWN_MIN <= drawdown_from_high <= DAY_DRAWDOWN_MAX):
            continue

        # ④ 거래량 감소 (눌림 3일 vs 직전 20일 비교 — 구간 분리)
        vol_3d = df["거래량"].iloc[i - 2:i + 1].mean()
        vol_prev = df["거래량"].iloc[i - 22:i - 2].mean()
        if vol_prev == 0 or vol_3d >= vol_prev * 0.7:
            continue

        # RSI 하드 필터: 과매수 구간 제외
        rsi_val = df["RSI"].iloc[i]
        if rsi_val > 70:
            continue

        atr = df["ATR"].iloc[i]
        signals.append({
            "idx": i,
            "tp_price": (
                min(close + atr * DAY_TP_MULT, close * (1 + DAY_MAX_PROFIT_PCT))
                if DAY_MAX_PROFIT_PCT is not None
                else close + atr * DAY_TP_MULT
            ),
            "sl_price": max(close - atr * DAY_SL_MULT, close * (1 - DAY_MAX_LOSS_PCT)),
            "rsi": round(float(rsi_val), 1),
            "pullback_pct": round(float(pullback_pct), 2),
            "vol_ratio": round(float(vol_3d / vol_prev), 2),
            "drawdown_from_high": round(float(drawdown_from_high), 2),
            "down_days": down_days,
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
        down_days = _count_down_days(df, i, window=5)
        if down_days < 3:
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

        # scanner.py scan_swing에는 필터로 쓰이지 않지만, 구간별 WIN율 분석용으로 기록
        rsi_val = df["RSI"].iloc[i]
        recent_high = df["고가"].iloc[max(0, i - 19):i + 1].max()
        drawdown_from_high = (close - recent_high) / recent_high * 100

        atr = df["ATR"].iloc[i]
        signals.append({
            "idx": i,
            "tp_price": close + atr * SWING_TP_MULT,
            "sl_price": max(close - atr * SWING_SL_MULT, close * (1 - SWING_MAX_LOSS_PCT)),
            "rsi": round(float(rsi_val), 1),
            "pullback_pct": round(float(pullback_pct), 2),
            "vol_ratio": round(float(vol_5d / vol_prev), 2),
            "drawdown_from_high": round(float(drawdown_from_high), 2),
            "down_days": down_days,
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
                for key in ("rsi", "pullback_pct", "vol_ratio", "drawdown_from_high", "down_days"):
                    trade[key] = sig[key]
                all_trades.append(trade)

    progress.empty()
    return _aggregate_results(all_trades)


GRID_TP_CAPS: list[float | None] = [None, 0.05, 0.06, 0.08, 0.10, 0.12]
GRID_SL_CAPS: list[float] = [0.03, 0.04, 0.05, 0.07]


def _prepare_day_df_dated(df_raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
    """_prepare_day_df와 동일하되, 구간 분할용으로 신호 인덱스에 대응하는 날짜를 함께 반환"""
    df = add_moving_averages(df_raw)
    df = add_atr(df)
    df = add_rsi(df)
    df = df.dropna(subset=["MA20", "MA60", "ATR", "RSI"])
    dates = pd.DatetimeIndex(df.index)
    return df.reset_index(drop=True), dates


def _summarize_trades(trades: list[dict]) -> dict[str, float]:
    """거래 목록 → 거래수/승률/평균 수익률(=기대값). 익절만 승리로 계산 (run_backtest와 동일 기준)"""
    if not trades:
        return {"거래수": 0, "승률(%)": 0.0, "기대값(%)": 0.0}
    profits = [t["profit_pct"] for t in trades]
    wins = sum(1 for t in trades if t["result"] == "익절")
    return {
        "거래수": len(trades),
        "승률(%)": round(wins / len(trades) * 100, 1),
        "기대값(%)": round(sum(profits) / len(profits), 2),
    }


def run_backtest_grid(
    months: int = 24,
    markets: list[str] | None = None,
    sample_size: int = 400,
    tp_caps: list[float | None] | None = None,
    sl_caps: list[float] | None = None,
) -> pd.DataFrame:
    """
    단기 전략 익절/손절 퍼센트 상한 조합 그리드 검증 (표본 확대 + 기간 분할).
    - 신호는 종목당 한 번만 탐지하고, 조합마다 익절가/손절가만 바꿔 시뮬레이션 (필터는 scanner.py와 동일)
    - 평가 구간(최근 months개월)을 앞 2/3(학습)과 뒤 1/3(검증)으로 나눠 따로 집계 —
      학습 구간 성적이 좋아도 검증 구간에서 무너지면 과거에 끼워 맞춘 값으로 판단
    - 수급 필터 제외, 슬리피지 0.1%, 동시 도달 시 손절 우선 (run_backtest와 동일)
    """
    markets = markets or ["KOSPI", "KOSDAQ"]
    tp_caps = GRID_TP_CAPS if tp_caps is None else tp_caps
    sl_caps = GRID_SL_CAPS if sl_caps is None else sl_caps

    now = datetime.now()
    end_date = now.strftime("%Y%m%d")
    start_date = _get_date_range(end_date, days=months * 30 + 150)
    window_start = pd.Timestamp(now - timedelta(days=months * 30))
    split_at = window_start + (pd.Timestamp(now) - window_start) * 2 / 3

    listings = [get_stock_listing(m) for m in markets]
    listings = [d for d in listings if not d.empty]
    if not listings:
        st.warning("종목 데이터를 가져올 수 없습니다.")
        return pd.DataFrame()

    universe = pd.concat(listings)
    universe = universe[~universe.index.duplicated()]
    universe = universe[universe["market_cap"] >= MIN_MARKET_CAP]
    if len(universe) > sample_size:
        universe = universe.sample(sample_size, random_state=42)
    tickers = universe.index.tolist()

    combos = [(tp, sl) for tp in tp_caps for sl in sl_caps]
    collected: dict[tuple[float | None, float], list[tuple[pd.Timestamp, dict]]] = {c: [] for c in combos}
    progress = st.progress(0, text="그리드 백테스트 진행 중...")

    for i, ticker in enumerate(tickers):
        progress.progress((i + 1) / len(tickers), text=f"그리드 백테스트: {ticker} ({i+1}/{len(tickers)})")

        df_raw = get_ohlcv(ticker, start_date, end_date)
        if len(df_raw) < 65:
            continue

        df, dates = _prepare_day_df_dated(df_raw)
        if len(df) < 10:
            continue

        for sig in _find_day_signals(df):
            idx = sig["idx"]
            sig_date = dates[idx]
            if sig_date < window_start:
                continue

            close = float(df["종가"].iloc[idx])
            atr = float(df["ATR"].iloc[idx])
            for tp_cap, sl_cap in combos:
                tp_price = close + atr * DAY_TP_MULT
                if tp_cap is not None:
                    tp_price = min(tp_price, close * (1 + tp_cap))
                sl_price = max(close - atr * DAY_SL_MULT, close * (1 - sl_cap))
                trade = _simulate_trade(df, idx, tp_price, sl_price, DAY_MAX_HOLD)
                if trade:
                    collected[(tp_cap, sl_cap)].append((sig_date, trade))

    progress.empty()

    rows: list[dict[str, float | str]] = []
    for (tp_cap, sl_cap), items in collected.items():
        all_trades = [t for _, t in items]
        train = [t for d, t in items if d < split_at]
        test = [t for d, t in items if d >= split_at]
        s_all, s_train, s_test = _summarize_trades(all_trades), _summarize_trades(train), _summarize_trades(test)
        rows.append({
            "익절 상한": "없음(ATR×2)" if tp_cap is None else f"{tp_cap * 100:.0f}%",
            "손절 상한": f"{sl_cap * 100:.0f}%",
            "전체 거래": s_all["거래수"],
            "전체 승률(%)": s_all["승률(%)"],
            "전체 기대값(%)": s_all["기대값(%)"],
            "학습 거래": s_train["거래수"],
            "학습 승률(%)": s_train["승률(%)"],
            "학습 기대값(%)": s_train["기대값(%)"],
            "검증 거래": s_test["거래수"],
            "검증 승률(%)": s_test["승률(%)"],
            "검증 기대값(%)": s_test["기대값(%)"],
        })

    return pd.DataFrame(rows)


# ── 신호 우위(Edge) 검증 ─────────────────────────────────────────────────────
# 익절/손절 튜닝 이전에 "이 신호가 애초에 시장 평균보다 나은가"를 먼저 확인한다.
# 청산 규칙(익절·손절)을 빼고 순수 5일 보유 수익률로 평가하고, 날짜별 교차단면 평균을 빼서
# 시장 방향(약세장 등)을 제거한 초과수익으로 비교한다.

EDGE_FEATURES: list[tuple[str, str]] = [
    ("pullback_pct", "MA20 대비 괴리(%)"),
    ("ma60_gap", "MA60 대비 괴리(%)"),
    ("ma60_slope", "MA60 1일 기울기(%)"),
    ("ma20_slope", "MA20 3일 기울기(%)"),
    ("drawdown20", "20일 고점 대비(%)"),
    ("ret5_past", "직전 5일 수익률(%)"),
    ("ret20_past", "직전 20일 수익률(%)"),
    ("rsi", "RSI(14)"),
    ("vol_ratio", "거래량비율(3일/직전20일)"),
    ("atr_pct", "변동성 ATR/종가(%)"),
    ("log_dollar_vol", "20일 평균 거래대금(log)"),
]
# 왕복 비용(%): 매수 슬리피지 + 수수료 양방향 + 증권거래세 (config.yaml fees 기준)
EDGE_COST_PCT = (_cfg["fees"]["commission"] * 2 + _cfg["fees"]["tax"] + SLIPPAGE) * 100


def _tstat(values: pd.Series) -> float:
    """평균/표준오차. 표본(블록)이 10개 미만이면 NaN"""
    v = values.dropna()
    if len(v) < 10 or v.std(ddof=1) == 0:
        return float("nan")
    return float(v.mean() / (v.std(ddof=1) / np.sqrt(len(v))))


def _build_edge_frame(
    df_raw: pd.DataFrame,
    ticker: str,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """종목 하나의 일별 피처 + 미래 수익률 + 신호 여부 (벡터화). 진입은 다음날 시가 기준."""
    full = add_rsi(add_atr(add_moving_averages(df_raw)))
    close, open_, high, vol = full["종가"], full["시가"], full["고가"], full["거래량"]

    feat = pd.DataFrame(index=full.index)
    feat["pullback_pct"] = (close / full["MA20"] - 1) * 100
    feat["ma60_gap"] = (close / full["MA60"] - 1) * 100
    feat["ma60_slope"] = (full["MA60"] / full["MA60"].shift(1) - 1) * 100
    feat["ma20_slope"] = (full["MA20"] / full["MA20"].shift(3) - 1) * 100
    feat["drawdown20"] = (close / high.rolling(20).max() - 1) * 100
    feat["ret5_past"] = (close / close.shift(5) - 1) * 100
    feat["ret20_past"] = (close / close.shift(20) - 1) * 100
    feat["rsi"] = full["RSI"]
    feat["vol_ratio"] = vol.rolling(3).mean() / vol.shift(3).rolling(20).mean()
    feat["atr_pct"] = full["ATR"] / close * 100
    feat["log_dollar_vol"] = np.log1p((close * vol).rolling(20).mean())

    entry = open_.shift(-1)  # 신호 다음날 시가
    feat["fwd3"] = (close.shift(-3) / entry - 1) * 100
    feat["fwd5"] = (close.shift(-5) / entry - 1) * 100        # 시가 매수 → 5일째 종가 청산
    feat["fwd5_cc"] = (close.shift(-5) / close - 1) * 100     # 신호일 종가 매수 → 5일째 종가 (갭 포함)
    feat["gap"] = (entry / close - 1) * 100                   # 신호일 종가 → 다음날 시가 (오버나이트)
    feat["fwd10"] = (close.shift(-10) / entry - 1) * 100
    feat["fwd20"] = (close.shift(-20) / entry - 1) * 100
    # 신호 구성 조건 (변형 신호 검증용, scanner.py 필터와 동일 정의)
    feat["down3"] = (close.diff() < 0).astype(float).rolling(3).sum()
    feat["ma20_up3"] = (full["MA20"] > full["MA20"].shift(1)) & (full["MA20"].shift(1) > full["MA20"].shift(2))
    feat["ma60_up"] = full["MA60"] > full["MA60"].shift(1)
    feat = feat.replace([np.inf, -np.inf], np.nan)

    # 신호 여부: scanner.py와 동일한 필터(_find_day_signals)를 그대로 재사용
    prepared, dates = _prepare_day_df_dated(df_raw)
    signal_dates = {dates[s["idx"]] for s in _find_day_signals(prepared)} if len(prepared) >= 23 else set()
    feat["signal"] = feat.index.isin(list(signal_dates))

    feat = feat[feat.index >= window_start]
    if window_end is not None:
        feat = feat[feat.index < window_end]
    feat = feat.dropna(subset=[c for c, _ in EDGE_FEATURES] + ["fwd5", "fwd5_cc", "gap"])
    feat["ticker"] = ticker
    feat.index.name = "date"
    return feat.reset_index()


def run_edge_study(
    months: int = 24,
    markets: list[str] | None = None,
    sample_size: int = 400,
) -> dict[str, pd.DataFrame]:
    """
    단기 눌림목 신호의 우위 검증.
    - headline: 신호일 vs 전체 종목-일(기준선) — 5일 보유 수익률, 비용 반영, 시장 대비 초과수익, t값
    - ic: 피처별 5일 후 초과수익과의 순위상관(IC)·5분위 수익률 (익절/손절과 무관한 순수 예측력)
    - regime: 시장 폭(MA60 위 종목 비율) 3분위별 기준선/신호 수익률
    t값은 5일 수익률이 날마다 겹치는 문제를 줄이기 위해 5거래일 블록 단위로 계산.
    """
    markets = markets or ["KOSPI", "KOSDAQ"]
    now = datetime.now()
    end_date = now.strftime("%Y%m%d")
    start_date = _get_date_range(end_date, days=months * 30 + 150)
    window_start = pd.Timestamp(now - timedelta(days=months * 30))
    split_at = window_start + (pd.Timestamp(now) - window_start) * 2 / 3

    listings = [get_stock_listing(m) for m in markets]
    listings = [d for d in listings if not d.empty]
    if not listings:
        st.warning("종목 데이터를 가져올 수 없습니다.")
        return {}

    universe = pd.concat(listings)
    universe = universe[~universe.index.duplicated()]
    universe = universe[universe["market_cap"] >= MIN_MARKET_CAP]
    if len(universe) > sample_size:
        universe = universe.sample(sample_size, random_state=42)
    tickers = universe.index.tolist()

    frames: list[pd.DataFrame] = []
    progress = st.progress(0, text="신호 우위 검증 진행 중...")
    for i, ticker in enumerate(tickers):
        progress.progress((i + 1) / len(tickers), text=f"신호 우위 검증: {ticker} ({i+1}/{len(tickers)})")
        df_raw = get_ohlcv(ticker, start_date, end_date)
        if len(df_raw) < 130:
            continue
        frame = _build_edge_frame(df_raw, ticker, window_start)
        if not frame.empty:
            frames.append(frame)
    progress.empty()

    if not frames:
        return {}

    all_df = pd.concat(frames, ignore_index=True)
    # 교차단면 평균이 불안정한 날(종목 수 부족) 제외
    all_df = all_df[all_df.groupby("date")["ticker"].transform("count") >= 50].copy()
    if all_df.empty:
        return {}

    # 시장 중립화: 날짜별 평균 수익률을 뺀 초과수익
    all_df["fwd5_x"] = all_df["fwd5"] - all_df.groupby("date")["fwd5"].transform("mean")

    # 5거래일 블록 (겹치는 수익률의 자기상관 보정용)
    unique_dates = sorted(all_df["date"].unique())
    block_map = {d: i // 5 for i, d in enumerate(unique_dates)}
    all_df["block"] = all_df["date"].map(block_map)
    is_train = all_df["date"] < split_at

    # ① 헤드라인: 기준선 vs 신호일
    head_rows: list[dict[str, float | str]] = []

    def add_head(label: str, mask: pd.Series) -> None:
        sub = all_df[mask]
        if sub.empty:
            return
        by_block = sub.groupby("block")["fwd5_x"].mean()
        head_rows.append({
            "구분": label,
            "표본": len(sub),
            "5일 평균수익(%)": round(sub["fwd5"].mean(), 2),
            "비용 반영(%)": round(sub["fwd5"].mean() - EDGE_COST_PCT, 2),
            "시장 대비 초과(%)": round(sub["fwd5_x"].mean(), 2),
            "초과 t값": round(_tstat(by_block), 2),
            "종가→종가 5일(%)": round(sub["fwd5_cc"].mean(), 2),
            "익일 갭(%)": round(sub["gap"].mean(), 2),
            "5일 상승비율(%)": round((sub["fwd5"] > 0).mean() * 100, 1),
        })

    all_true = pd.Series(True, index=all_df.index)
    sig = all_df["signal"]
    add_head("전체 종목-일 (기준선)", all_true)
    add_head("신호일 (v3/v4 필터)", sig)
    add_head("신호일 · 학습구간", sig & is_train)
    add_head("신호일 · 검증구간", sig & ~is_train)

    # ② 피처별 IC + 5분위
    by_date = all_df.groupby("date")
    r_fwd = by_date["fwd5_x"].rank(pct=True)
    block_of_date = all_df.drop_duplicates("date").set_index("date")["block"]
    ic_rows: list[dict[str, float | str]] = []
    for col, label in EDGE_FEATURES:
        r_feat = by_date[col].rank(pct=True)
        tmp = pd.DataFrame({"date": all_df["date"], "a": r_feat, "b": r_fwd})
        ic_by_date = tmp.groupby("date")[["a", "b"]].apply(lambda x: x["a"].corr(x["b"]))
        ic_block = ic_by_date.groupby(block_of_date.reindex(ic_by_date.index).values).mean()
        bucket = np.ceil(r_feat * 5).clip(1, 5)
        quint = all_df.groupby(bucket)["fwd5_x"].mean()
        q1 = float(quint.get(1.0, np.nan))
        q5 = float(quint.get(5.0, np.nan))
        ic_rows.append({
            "피처": label,
            "평균 IC(%)": round(float(ic_by_date.mean()) * 100, 2),
            "IC t값": round(_tstat(ic_block), 2),
            "학습 IC(%)": round(float(ic_by_date[ic_by_date.index < split_at].mean()) * 100, 2),
            "검증 IC(%)": round(float(ic_by_date[ic_by_date.index >= split_at].mean()) * 100, 2),
            "Q1(하위20%) 초과(%)": round(q1, 2),
            "Q5(상위20%) 초과(%)": round(q5, 2),
            "Q5−Q1(%)": round(q5 - q1, 2),
        })

    # ③ 장세별: 시장 폭(MA60 위 종목 비율) 3분위
    all_df["above60"] = all_df["ma60_gap"] > 0
    breadth = all_df.groupby("date")["above60"].mean()
    tercile = pd.qcut(breadth, 3, labels=False, duplicates="drop")
    names = {0: "약세장(폭 하위1/3)", 1: "중립", 2: "강세장(폭 상위1/3)"}
    all_df["regime"] = all_df["date"].map(tercile)
    regime_rows: list[dict[str, float | str]] = []
    for code in sorted(c for c in all_df["regime"].dropna().unique()):
        sub = all_df[all_df["regime"] == code]
        sig_sub = sub[sub["signal"]]
        b_range = breadth[tercile == code]
        regime_rows.append({
            "장세": names.get(int(code), str(code)),
            "MA60 위 종목비율(%)": f"{b_range.min() * 100:.0f}~{b_range.max() * 100:.0f}",
            "기준선 5일수익(%)": round(sub["fwd5"].mean(), 2),
            "신호 표본": len(sig_sub),
            "신호 5일수익(%)": round(sig_sub["fwd5"].mean(), 2) if len(sig_sub) else float("nan"),
            "신호 시장대비 초과(%)": round(sig_sub["fwd5_x"].mean(), 2) if len(sig_sub) else float("nan"),
        })

    meta = pd.DataFrame([{
        "거래일 수": len(unique_dates),
        "종목-일 표본": len(all_df),
        "종목 수": all_df["ticker"].nunique(),
        "비용(%)": round(EDGE_COST_PCT, 2),
    }])
    return {
        "meta": meta,
        "headline": pd.DataFrame(head_rows),
        "ic": pd.DataFrame(ic_rows),
        "regime": pd.DataFrame(regime_rows),
    }


# ── 변형 신호 검증 (사전 등록) ────────────────────────────────────────────────
# 검정 수를 사전에 고정(변형 7 × 보유 3 = 21)하고, 다중검정 보정으로 비용 후 t값 3 이상을 합격선으로 둔다.
# 합격 조건: 비용 반영 후 시장 대비 초과수익 > 0, 비용 후 t값 ≥ 3, 학습·검증 구간 모두 > 0.

VARIANT_HORIZONS: list[int] = [5, 10, 20]
VARIANT_T_PASS = 3.0
VARIANT_T_WATCH = 2.0
VARIANT_MIN_DOLLAR_VOL = 5_000_000_000  # 20일 평균 거래대금 50억 미만은 체결 가능성이 낮아 제외


def _collect_edge_frames(
    months: int,
    markets: list[str] | None,
    sample_size: int,
    end_offset_months: int = 0,
) -> tuple[pd.DataFrame, pd.Timestamp] | None:
    """
    종목별 피처/수익률 프레임을 모아 하나로 합침. (전체 프레임, 학습/검증 분할 시점) 반환.
    평가 구간 = [오늘-months개월, 오늘-end_offset_months개월). end_offset_months로 최근 구간을 제외하면
    최근 데이터를 보고 만든 가설을 그 이전 기간(새 데이터)으로 확인할 수 있다.
    """
    markets = markets or ["KOSPI", "KOSDAQ"]
    now = datetime.now()
    end_date = now.strftime("%Y%m%d")
    start_date = _get_date_range(end_date, days=months * 30 + 150)
    window_start = pd.Timestamp(now - timedelta(days=months * 30))
    window_end = pd.Timestamp(now - timedelta(days=end_offset_months * 30))
    split_at = window_start + (window_end - window_start) * 2 / 3

    listings = [get_stock_listing(m) for m in markets]
    listings = [d for d in listings if not d.empty]
    if not listings:
        st.warning("종목 데이터를 가져올 수 없습니다.")
        return None

    universe = pd.concat(listings)
    universe = universe[~universe.index.duplicated()]
    universe = universe[universe["market_cap"] >= MIN_MARKET_CAP]
    if len(universe) > sample_size:
        universe = universe.sample(sample_size, random_state=42)
    tickers = universe.index.tolist()

    frames: list[pd.DataFrame] = []
    progress = st.progress(0, text="변형 신호 검증 진행 중...")
    for i, ticker in enumerate(tickers):
        progress.progress((i + 1) / len(tickers), text=f"변형 신호 검증: {ticker} ({i+1}/{len(tickers)})")
        df_raw = get_ohlcv(ticker, start_date, end_date)
        if len(df_raw) < 130:
            continue
        frame = _build_edge_frame(df_raw, ticker, window_start, window_end)
        if not frame.empty:
            frames.append(frame)
    progress.empty()

    if not frames:
        return None
    return pd.concat(frames, ignore_index=True), split_at


def run_variant_study(
    months: int = 24,
    markets: list[str] | None = None,
    sample_size: int = 400,
    end_offset_months: int = 0,
) -> dict[str, pd.DataFrame]:
    """
    변형 신호 7종 × 보유 5/10/20일 검증 (거래 가능 유니버스 한정, 시장 대비 초과수익).
    - V0 현재 신호, V1 깊은눌림 밴드 제거, V2 + 거래량수축 제거, V3 얕은 눌림(-8% 이내),
      V4 저변동·고점근접·추세, V5 V4+거래량 증가, V6 고점근접+저변동 복합 상위 10%
    - 시장 = 같은 날 거래 가능 유니버스 전체 평균. t값은 보유일 길이 블록 단위(겹침 보정), 비용 반영 후 기준.
    """
    loaded = _collect_edge_frames(months, markets, sample_size, end_offset_months)
    if loaded is None:
        return {}
    all_df, split_at = loaded

    # 거래 가능 유니버스 + 교차단면 안정성(하루 30종목 이상)
    all_df = all_df[all_df["log_dollar_vol"] >= np.log1p(VARIANT_MIN_DOLLAR_VOL)].copy()
    all_df = all_df[all_df.groupby("date")["ticker"].transform("count") >= 30].copy()
    if all_df.empty:
        return {}

    by_date = all_df.groupby("date")
    atr_rank = by_date["atr_pct"].rank(pct=True)
    dd_rank = by_date["drawdown20"].rank(pct=True)
    all_df["_score"] = dd_rank + (1 - atr_rank)
    score_rank = all_df.groupby("date")["_score"].rank(pct=True)

    trend = (all_df["ma60_gap"] > 0) & all_df["ma60_up"]
    pull = (
        (all_df["down3"] >= 2)
        & all_df["pullback_pct"].between(-PULLBACK_BAND_PCT, 1.0)
        & all_df["ma20_up3"]
    )
    vol_contract = all_df["vol_ratio"] < 0.7
    rsi_ok = all_df["rsi"] <= 70
    near_high = all_df["drawdown20"] >= -5
    low_vol = atr_rank <= 1 / 3

    variants: dict[str, pd.Series] = {
        "V0 현재 신호(v3/v4)": all_df["signal"],
        "V1 깊은눌림 밴드 제거": trend & pull & vol_contract & rsi_ok,
        "V2 V1+거래량수축 제거": trend & pull & rsi_ok,
        "V3 얕은 눌림(고점대비 -8% 이내)": trend & pull & rsi_ok & (all_df["drawdown20"] >= -8),
        "V4 저변동·고점근접·추세": trend & low_vol & near_high,
        "V5 V4+거래량 증가": trend & low_vol & near_high & (all_df["vol_ratio"] > 1),
        "V6 고점근접+저변동 상위10%": score_rank >= 0.9,
    }

    unique_dates = sorted(all_df["date"].unique())
    rank_map = {d: i for i, d in enumerate(unique_dates)}
    all_df["_rank"] = all_df["date"].map(rank_map)
    is_train = all_df["date"] < split_at

    for h in VARIANT_HORIZONS:
        col = f"fwd{h}"
        all_df[f"x{h}"] = all_df[col] - all_df.groupby("date")[col].transform("mean")

    rows: list[dict[str, float | str]] = []
    for name, mask in variants.items():
        for h in VARIANT_HORIZONS:
            valid = mask & all_df[f"fwd{h}"].notna()
            sub = all_df[valid]
            if len(sub) < 30:
                continue
            x = sub[f"x{h}"]
            block_means = x.groupby(sub["_rank"] // h).mean()
            net_x = float(x.mean()) - EDGE_COST_PCT
            t_net = _tstat(block_means - EDGE_COST_PCT)
            train_net = float(sub.loc[is_train[valid], f"x{h}"].mean()) - EDGE_COST_PCT
            test_net = float(sub.loc[~is_train[valid], f"x{h}"].mean()) - EDGE_COST_PCT
            halves_ok = train_net > 0 and test_net > 0
            if net_x > 0 and t_net >= VARIANT_T_PASS and halves_ok:
                verdict = "합격"
            elif net_x > 0 and t_net >= VARIANT_T_WATCH and halves_ok:
                verdict = "관찰(t 2~3)"
            else:
                verdict = "탈락"
            rows.append({
                "변형": name,
                "보유(일)": h,
                "표본": len(sub),
                "일평균 신호": round(len(sub) / len(unique_dates), 2),
                "평균수익(%)": round(float(sub[f"fwd{h}"].mean()), 2),
                "비용후 초과(%)": round(net_x, 2),
                "비용후 t값": round(t_net, 2),
                "학습 비용후 초과(%)": round(train_net, 2),
                "검증 비용후 초과(%)": round(test_net, 2),
                "판정": verdict,
            })

    market_rows = [
        {
            "보유(일)": h,
            "시장 평균수익(%)": round(float(all_df[f"fwd{h}"].mean()), 2),
            "비용 후(%)": round(float(all_df[f"fwd{h}"].mean()) - EDGE_COST_PCT, 2),
        }
        for h in VARIANT_HORIZONS
    ]
    meta = pd.DataFrame([{
        "거래일 수": len(unique_dates),
        "거래가능 종목-일": len(all_df),
        "종목 수": all_df["ticker"].nunique(),
        "검정 수": len(variants) * len(VARIANT_HORIZONS),
        "합격선 t": VARIANT_T_PASS,
    }])
    return {"meta": meta, "market": pd.DataFrame(market_rows), "variants": pd.DataFrame(rows)}
