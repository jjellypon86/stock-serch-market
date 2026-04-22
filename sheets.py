from __future__ import annotations

from datetime import datetime, timedelta

import FinanceDataReader as fdr
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

try:
    import gspread
    _GSPREAD_OK = True
except ImportError:
    _GSPREAD_OK = False

SHEET_NAME = "K-Quant Tracker"
HIST_WS    = "history"
SCOPES     = ["https://www.googleapis.com/auth/spreadsheets"]

COLUMNS = [
    "scan_date", "strategy", "market", "ticker", "name",
    "buy_price", "entry_price",
    "take_profit", "stop_loss", "risk_reward", "pullback_pct",
    "inst_days", "foreign_days",
    "result", "profit_pct", "hold_days",
    "actual_buy",
]

DAY_MAX_HOLD   = 5
SWING_MAX_HOLD = 10
SLIPPAGE       = 0.001


def _is_configured() -> bool:
    return _GSPREAD_OK and "gcp_service_account" in st.secrets


def _get_worksheet():
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPES
    )
    gc = gspread.authorize(creds)
    sh = gc.open(SHEET_NAME)
    try:
        ws = sh.worksheet(HIST_WS)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=HIST_WS, rows=1000, cols=len(COLUMNS))
        ws.append_row(COLUMNS)
    return ws


def save_scan_results(df: pd.DataFrame, strategy: str, market: str, scan_date: str) -> bool:
    """스캔 완료 즉시 Sheets에 추가. 결과 컬럼은 공란."""
    if not _is_configured():
        return False
    try:
        ws = _get_worksheet()
        rows = [
            [
                scan_date, strategy, market,
                str(r["ticker"]), r["name"],
                int(r["buy_price"]), "",
                int(r["take_profit"]), int(r["stop_loss"]),
                float(r["risk_reward"]), round(float(r["pullback_pct"]), 2),
                int(r.get("inst_days", 0)), int(r.get("foreign_days", 0)),
                "", "", "",
                "",
            ]
            for _, r in df.iterrows()
        ]
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        return True
    except Exception:
        return False


def _next_trading_day(date_str: str) -> str:
    """YYYY-MM-DD 형식 날짜의 다음 거래일 반환"""
    d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _calc_result(
    ticker: str,
    entry_date: str,
    take_profit: float,
    stop_loss: float,
    max_hold: int,
) -> tuple[str, float, int, float]:
    """
    OHLCV로 결과 자동 판정.
    반환: (entry_price, result, profit_pct, hold_days)
    """
    try:
        start = entry_date
        end = (datetime.strptime(entry_date, "%Y-%m-%d") + timedelta(days=max_hold + 10)).strftime("%Y-%m-%d")
        df = fdr.DataReader(ticker, start, end)
        if df.empty:
            return "", "PENDING", 0.0, 0

        entry_price = float(df["Open"].iloc[0]) * (1 + SLIPPAGE)

        trading_days = 0
        for i, (_, row) in enumerate(df.iterrows()):
            if i == 0:
                high = float(row["High"])
                low  = float(row["Low"])
            else:
                trading_days += 1
                high = float(row["High"])
                low  = float(row["Low"])

            if high >= take_profit:
                profit_pct = round((take_profit / entry_price - 1) * 100, 2)
                return entry_price, "WIN", profit_pct, trading_days
            if low <= stop_loss:
                profit_pct = round((stop_loss / entry_price - 1) * 100, 2)
                return entry_price, "LOSS", profit_pct, trading_days

            if trading_days >= max_hold:
                close = float(row["Close"])
                profit_pct = round((close / entry_price - 1) * 100, 2)
                return entry_price, "EXPIRED", profit_pct, trading_days

        return entry_price, "PENDING", 0.0, trading_days
    except Exception:
        return "", "ERROR", 0.0, 0


def update_results() -> int:
    """
    result 컬럼이 비어 있는 행을 FDR로 자동 판정해 Sheets 업데이트.
    반환: 업데이트된 행 수
    """
    if not _is_configured():
        return 0
    try:
        ws = _get_worksheet()
        all_rows = ws.get_all_records()
        if not all_rows:
            return 0

        updated = 0
        for i, row in enumerate(all_rows):
            if row.get("result") not in ("", None):
                continue

            scan_date = str(row.get("scan_date", ""))
            if not scan_date:
                continue

            try:
                entry_date = _next_trading_day(scan_date)
            except ValueError:
                continue

            strategy   = str(row.get("strategy", "day"))
            max_hold   = DAY_MAX_HOLD if strategy == "day" else SWING_MAX_HOLD
            ticker     = str(row.get("ticker", ""))
            take_profit = float(row.get("take_profit", 0))
            stop_loss   = float(row.get("stop_loss", 0))

            entry_price, result, profit_pct, hold_days = _calc_result(
                ticker, entry_date, take_profit, stop_loss, max_hold
            )
            if result == "PENDING":
                continue

            sheet_row = i + 2  # 헤더 1행 + 0-index 보정
            col_entry   = COLUMNS.index("entry_price") + 1
            col_result  = COLUMNS.index("result") + 1
            col_profit  = COLUMNS.index("profit_pct") + 1
            col_hold    = COLUMNS.index("hold_days") + 1

            ws.update_cell(sheet_row, col_entry,  round(entry_price, 0) if entry_price else "")
            ws.update_cell(sheet_row, col_result,  result)
            ws.update_cell(sheet_row, col_profit,  profit_pct)
            ws.update_cell(sheet_row, col_hold,    hold_days)
            updated += 1

        return updated
    except Exception:
        return 0


def load_history() -> pd.DataFrame:
    """Sheets 전체 히스토리 로드."""
    if not _is_configured():
        return pd.DataFrame()
    try:
        ws = _get_worksheet()
        records = ws.get_all_records()
        return pd.DataFrame(records) if records else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def is_configured() -> bool:
    return _is_configured()
