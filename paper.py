"""
모의 추적(포워드 테스트) — 저변동·고점근접·추세 후보 (실제 매매 없음)

백테스트(24개월 + 이전 36개월)에서 방향은 일관되지만 통계적으로 입증되지 않은 후보(V4)를
앞으로의 데이터로 검증한다. 매일 후보를 별도 워크시트(paper_lowvol)에 기록하고,
신호 후 20거래일이 지나면 "다음날 시가 매수 → 20거래일째 종가 청산" 수익률을
같은 기간 시장지수(KOSPI=KS11, KOSDAQ=KQ11) 수익률과 비교해 비용 반영 초과수익으로 판정한다.
기존 history 시트·판정 로직과는 완전히 분리되어 있다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import FinanceDataReader as fdr
import numpy as np
import pandas as pd
import streamlit as st
import yaml

from scanner import _get_date_range
from sheets import SCOPES, SHEET_NAME, _is_configured
from utils import add_atr, add_moving_averages, get_ohlcv, get_stock_listing

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:  # sheets._is_configured()가 False를 반환하므로 이후 시트 접근은 일어나지 않음
    gspread = None  # type: ignore[assignment]
    Credentials = None  # type: ignore[assignment,misc]

with open("config.yaml") as _f:
    _cfg = yaml.safe_load(_f)

_P = _cfg["paper_lowvol"]
HOLD_DAYS: int = int(_P["hold_days"])
DRAWDOWN_MIN: float = float(_P["drawdown_min"])
ATR_RANK_MAX: float = float(_P["atr_rank_max"])
MIN_MARKET_CAP: int = int(_P["min_market_cap"])
MIN_TRADE_VALUE: int = int(_P["min_trade_value"])
TAG: str = str(_P["tag"])

SLIPPAGE: float = float(_cfg["backtest"]["slippage"])
# 왕복 비용(%): 슬리피지 + 수수료 양방향 + 증권거래세 (백테스트와 동일 기준)
COST_PCT: float = (_cfg["fees"]["commission"] * 2 + _cfg["fees"]["tax"] + SLIPPAGE) * 100

PAPER_WS = "paper_lowvol"
INDEX_CODE = {"KOSPI": "KS11", "KOSDAQ": "KQ11"}

PAPER_COLUMNS = [
    "scan_date", "ticker", "name", "market",
    "close", "atr_pct", "drawdown20", "ma60_gap", "vol_ratio",
    "entry_date", "entry_price", "exit_date", "exit_price",
    "ret_pct", "bench_ret_pct", "excess_pct", "net_excess_pct",
    "result", "tag",
]
_FIRST_RESULT_COL = PAPER_COLUMNS.index("entry_date") + 1
_LAST_RESULT_COL = PAPER_COLUMNS.index("result") + 1
DONE_RESULTS = ("OUTPERFORM", "UNDERPERFORM")


def _norm_ticker(raw: object) -> str:
    """시트에서 숫자(5930.0)나 문자열로 읽힌 종목코드를 6자리 문자열로 통일"""
    try:
        return str(int(float(str(raw)))).zfill(6)
    except (ValueError, TypeError):
        return str(raw).strip().zfill(6)


def _get_paper_worksheet():
    creds = Credentials.from_service_account_info(st.secrets["gcp_service_account"], scopes=SCOPES)
    sh = gspread.authorize(creds).open(SHEET_NAME)
    try:
        return sh.worksheet(PAPER_WS)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=PAPER_WS, rows=1000, cols=len(PAPER_COLUMNS))
        ws.append_row(PAPER_COLUMNS)
        return ws


def scan_lowvol_trend(date: str, markets: list[str]) -> pd.DataFrame:
    """
    저변동·고점근접·추세 후보 스캔 (date 종가 기준, 백테스트 V4와 동일 정의)
    - 유니버스: 시총·거래대금 하한 통과 종목 (단기 스캔과 동일 → 시세 캐시 재사용)
    - 추세: 종가 > MA60 이고 MA60 상승
    - 고점근접: 20일 고점 대비 하락률 ≥ DRAWDOWN_MIN(%)
    - 저변동: 유니버스 내 ATR/종가(%) 하위 ATR_RANK_MAX 분위 이하
    """
    start = _get_date_range(date, days=150)
    records: list[dict[str, object]] = []

    for market in markets:
        listing = get_stock_listing(market)
        if listing.empty:
            continue
        pool = listing[(listing["market_cap"] >= MIN_MARKET_CAP) & (listing["거래대금"] >= MIN_TRADE_VALUE)]
        for ticker in pool.index.tolist():
            df = get_ohlcv(ticker, start, date)
            if len(df) < 65 or df.index[-1].strftime("%Y%m%d") != date:
                continue
            full = add_atr(add_moving_averages(df))
            last, prev = full.iloc[-1], full.iloc[-2]
            close = float(last["종가"])
            ma60, atr, prev_ma60 = last["MA60"], last["ATR"], prev["MA60"]
            if close <= 0 or pd.isna(ma60) or pd.isna(atr) or pd.isna(prev_ma60):
                continue
            high20 = float(full["고가"].iloc[-20:].max())
            vol_prev = float(full["거래량"].iloc[-23:-3].mean())
            vol_3d = float(full["거래량"].iloc[-3:].mean())
            records.append({
                "ticker": ticker,
                "name": pool.loc[ticker, "name"],
                "market": market,
                "close": int(close),
                "atr_pct": round(float(atr) / close * 100, 2),
                "drawdown20": round((close / high20 - 1) * 100, 2),
                "ma60_gap": round((close / float(ma60) - 1) * 100, 2),
                "vol_ratio": round(vol_3d / vol_prev, 2) if vol_prev > 0 else float("nan"),
                "trend": close > float(ma60) and float(ma60) > float(prev_ma60),
            })

    if not records:
        return pd.DataFrame()

    universe = pd.DataFrame(records)
    atr_cut = float(universe["atr_pct"].quantile(ATR_RANK_MAX))
    picked = universe[
        universe["trend"] & (universe["drawdown20"] >= DRAWDOWN_MIN) & (universe["atr_pct"] <= atr_cut)
    ].copy()
    picked["universe_n"] = len(universe)
    picked["atr_cut"] = round(atr_cut, 2)
    return picked.drop(columns=["trend"]).sort_values("atr_pct").reset_index(drop=True)


def save_paper_signals(df: pd.DataFrame, scan_date: str) -> tuple[bool, str, int, int]:
    """후보를 paper_lowvol 시트에 추가 (scan_date+종목 중복은 건너뜀). 반환: (성공, 에러, 저장수, 중복수)"""
    if not _is_configured():
        return False, "gcp_service_account secrets 미설정", 0, 0
    try:
        ws = _get_paper_worksheet()
        existing = {
            (str(r.get("scan_date", "")), _norm_ticker(r.get("ticker", "")))
            for r in ws.get_all_records()
        }
        rows: list[list[object]] = []
        skipped = 0
        for _, r in df.iterrows():
            ticker = _norm_ticker(r["ticker"])
            if (scan_date, ticker) in existing:
                skipped += 1
                continue
            rows.append([
                scan_date, "'" + ticker, r["name"], r["market"],
                int(r["close"]), float(r["atr_pct"]), float(r["drawdown20"]), float(r["ma60_gap"]),
                "" if pd.isna(r["vol_ratio"]) else float(r["vol_ratio"]),
                "", "", "", "", "", "", "", "", "",
                TAG,
            ])
        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
        return True, "", len(rows), skipped
    except Exception as e:
        return False, str(e), 0, 0


def _evaluate_one(ticker: str, market: str, scan_date: str) -> list[object] | None:
    """
    신호 후 HOLD_DAYS 거래일이 지났으면 결과 행(entry_date~result 9칸)을 반환, 아니면 None.
    진입 = 신호 다음 거래일 시가, 청산 = 신호일 기준 HOLD_DAYS번째 거래일 종가 (백테스트 fwd20과 동일).
    초과수익 = 종목 수익률 − 같은 기간 시장지수 수익률, 순초과 = 초과 − 왕복 비용.
    """
    signal_day = datetime.strptime(scan_date, "%Y%m%d")
    start = (signal_day - timedelta(days=5)).strftime("%Y-%m-%d")
    end = (signal_day + timedelta(days=HOLD_DAYS * 2 + 14)).strftime("%Y-%m-%d")

    df = fdr.DataReader(ticker, start, end)
    if df.empty:
        return None
    labels = list(df.index.strftime("%Y%m%d"))
    if scan_date not in labels:
        return None
    pos = labels.index(scan_date)
    if len(df) <= pos + HOLD_DAYS:
        return None  # 아직 HOLD_DAYS 거래일이 지나지 않음

    entry_ts, exit_ts = df.index[pos + 1], df.index[pos + HOLD_DAYS]
    entry_open = float(df["Open"].iloc[pos + 1])
    exit_close = float(df["Close"].iloc[pos + HOLD_DAYS])
    if entry_open <= 0:
        return None
    ret = (exit_close / entry_open - 1) * 100

    bench = fdr.DataReader(INDEX_CODE.get(market, "KS11"), start, end)
    if entry_ts not in bench.index or exit_ts not in bench.index:
        return None
    b_open = float(bench.loc[entry_ts, "Open"])
    if b_open <= 0:
        b_open = float(bench.loc[entry_ts, "Close"])
    b_ret = (float(bench.loc[exit_ts, "Close"]) / b_open - 1) * 100

    excess = ret - b_ret
    net_excess = excess - COST_PCT
    return [
        entry_ts.strftime("%Y%m%d"), round(entry_open * (1 + SLIPPAGE)), exit_ts.strftime("%Y%m%d"), round(exit_close),
        round(ret, 2), round(b_ret, 2), round(excess, 2), round(net_excess, 2),
        "OUTPERFORM" if net_excess > 0 else "UNDERPERFORM",
    ]


def update_paper_results() -> tuple[int, str]:
    """결과 칸이 빈 행 중 HOLD_DAYS 거래일이 지난 것을 판정해 시트에 기록. 반환: (판정 수, 에러)"""
    if not _is_configured():
        return 0, ""
    try:
        ws = _get_paper_worksheet()
        updated = 0
        for i, row in enumerate(ws.get_all_records()):
            if str(row.get("result", "")).strip() in DONE_RESULTS:
                continue
            scan_date = str(row.get("scan_date", "")).strip()
            if not scan_date:
                continue
            result_cells = _evaluate_one(_norm_ticker(row.get("ticker", "")), str(row.get("market", "KOSPI")), scan_date)
            if result_cells is None:
                continue
            sheet_row = i + 2  # 헤더 1행 + 0-index 보정
            first = gspread.utils.rowcol_to_a1(sheet_row, _FIRST_RESULT_COL)
            last = gspread.utils.rowcol_to_a1(sheet_row, _LAST_RESULT_COL)
            ws.update(range_name=f"{first}:{last}", values=[result_cells])
            updated += 1
        return updated, ""
    except Exception as e:
        return 0, str(e)


@st.cache_data(ttl=300)
def load_paper_history() -> pd.DataFrame:
    if not _is_configured():
        return pd.DataFrame()
    try:
        records = _get_paper_worksheet().get_all_records()
    except Exception:
        return pd.DataFrame()
    if not records:
        return pd.DataFrame(columns=PAPER_COLUMNS)
    df = pd.DataFrame(records)
    df["ticker"] = df["ticker"].map(_norm_ticker)
    return df


def summarize_paper(df: pd.DataFrame) -> dict[str, float]:
    """추적 현황 요약. t값은 28일 블록(겹치는 20거래일 수익률 보정) 5개 이상일 때만 계산"""
    if df.empty:
        return {}
    d = df.copy()
    for col in ("ret_pct", "bench_ret_pct", "excess_pct", "net_excess_pct"):
        d[col] = pd.to_numeric(d[col], errors="coerce")
    done = d[d["result"].isin(DONE_RESULTS)]
    out: dict[str, float] = {"총 추적": float(len(d)), "판정 완료": float(len(done)), "대기": float(len(d) - len(done))}
    if done.empty:
        return out

    ordinal = pd.to_datetime(done["scan_date"].astype(str), format="%Y%m%d").map(pd.Timestamp.toordinal)
    block_means = done["net_excess_pct"].groupby(ordinal // 28).mean()
    t_value = float("nan")
    if len(block_means) >= 5 and block_means.std(ddof=1) > 0:
        t_value = float(block_means.mean() / (block_means.std(ddof=1) / np.sqrt(len(block_means))))
    out.update({
        "평균 수익률(%)": round(float(done["ret_pct"].mean()), 2),
        "평균 시장지수(%)": round(float(done["bench_ret_pct"].mean()), 2),
        "평균 비용후 초과(%)": round(float(done["net_excess_pct"].mean()), 2),
        "초과 플러스 비율(%)": round(float((done["net_excess_pct"] > 0).mean() * 100), 1),
        "t값(28일 블록)": round(t_value, 2),
    })
    return out
