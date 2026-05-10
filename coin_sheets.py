from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

try:
  from google.oauth2.service_account import Credentials
  import gspread
  _GSPREAD_OK = True
except ImportError:
  _GSPREAD_OK = False

try:
  import pybithumb
  _PYBITHUMB_OK = True
except ImportError:
  _PYBITHUMB_OK = False

# ── 상수 ──────────────────────────────────────────────────────────────────────
SHEET_NAME     = "K-Quant Tracker"   # 주식 시스템과 동일 스프레드시트
COIN_WS        = "coin_history"      # 코인 전용 워크시트 (주식 "history"와 분리)
SCOPES         = [
  "https://www.googleapis.com/auth/spreadsheets",
  "https://www.googleapis.com/auth/drive",
]
SLIPPAGE       = 0.001
DAY_MAX_HOLD   = 5
SWING_MAX_HOLD = 10

COLUMNS: list[str] = [
  "scan_date", "strategy", "ticker", "name",
  "buy_price", "entry_price",
  "take_profit", "stop_loss", "risk_reward", "pullback_pct",
  "btc_direction",
  "result", "profit_pct", "hold_days",
  "actual_buy",
]


# ── 내부 유틸 ─────────────────────────────────────────────────────────────────

def _is_configured() -> bool:
  """gspread 패키지와 시크릿 설정 여부 확인"""
  return _GSPREAD_OK and "gcp_service_account" in st.secrets


def _get_worksheet() -> "gspread.Worksheet":
  """코인 전용 워크시트 반환 (없으면 신규 생성 후 헤더 추가)"""
  creds = Credentials.from_service_account_info(
    st.secrets["gcp_service_account"], scopes=SCOPES
  )
  gc = gspread.authorize(creds)
  sh = gc.open(SHEET_NAME)
  try:
    ws = sh.worksheet(COIN_WS)
  except gspread.WorksheetNotFound:
    ws = sh.add_worksheet(title=COIN_WS, rows=1000, cols=len(COLUMNS))
    ws.append_row(COLUMNS)
  return ws


def _next_day(date_str: str) -> str:
  """
  코인은 24/7 거래이므로 단순 +1일 반환 (주말 처리 불필요).
  입력: "YYYY-MM-DD" 또는 "YYYYMMDD"
  반환: "YYYY-MM-DD"
  """
  fmt = "%Y%m%d" if "-" not in date_str else "%Y-%m-%d"
  d = datetime.strptime(date_str, fmt) + timedelta(days=1)
  return d.strftime("%Y-%m-%d")


def _calc_coin_result(
  ticker: str,
  entry_date_str: str,
  take_profit: float,
  stop_loss: float,
  max_hold_days: int,
) -> tuple[float, str, float, int]:
  """
  pybithumb으로 OHLCV 조회 후 거래 결과 자동 판정.

  코인은 24/7 거래이므로 스캔 당일 즉시 진입 가정:
  - 첫째 날(i==0, scan_date): entry_price = close * (1 + SLIPPAGE)
  - 이후 날:
      high >= take_profit → WIN
      low  <= stop_loss   → LOSS
      hold_days >= max_hold_days → EXPIRED (종가 기준 profit_pct)

  반환: (entry_price, result, profit_pct, hold_days)
  예외 시: (0.0, "ERROR", 0.0, 0)
  """
  if not _PYBITHUMB_OK:
    return 0.0, "ERROR", 0.0, 0
  try:
    df = pybithumb.get_candlestick(ticker, "KRW", "24h")
    if df is None or df.empty:
      return 0.0, "ERROR", 0.0, 0

    # scan_date 이후 데이터만 추출
    entry_dt = datetime.strptime(entry_date_str, "%Y-%m-%d")
    df = df[df.index >= entry_dt].copy()
    if df.empty:
      return 0.0, "PENDING", 0.0, 0

    entry_price = 0.0
    hold_days   = 0

    for i, (_, row) in enumerate(df.iterrows()):
      high  = float(row["high"])
      low   = float(row["low"])
      close = float(row["close"])

      if i == 0:
        # 스캔 당일 종가로 즉시 진입 (슬리피지 반영)
        entry_price = close * (1 + SLIPPAGE)
        continue

      hold_days += 1

      # 목표가 도달 → WIN
      if high >= take_profit:
        profit_pct = round((take_profit / entry_price - 1) * 100, 2)
        return entry_price, "WIN", profit_pct, hold_days

      # 손절가 도달 → LOSS
      if low <= stop_loss:
        profit_pct = round((stop_loss / entry_price - 1) * 100, 2)
        return entry_price, "LOSS", profit_pct, hold_days

      # 최대 보유일 도달 → EXPIRED
      if hold_days >= max_hold_days:
        profit_pct = round((close / entry_price - 1) * 100, 2)
        return entry_price, "EXPIRED", profit_pct, hold_days

    # 데이터 소진 — 아직 판정 불가
    return entry_price, "PENDING", 0.0, hold_days

  except Exception:
    return 0.0, "ERROR", 0.0, 0


# ── 공개 API ──────────────────────────────────────────────────────────────────

def save_coin_scan_results(
  df: pd.DataFrame,
  strategy: str,
  scan_date: str,
) -> tuple[bool, str, int, int]:
  """
  코인 스캔 결과를 Sheets에 저장. 결과 컬럼(entry_price, result 등)은 공란.
  중복 체크 기준: (scan_date, strategy, ticker)

  반환: (성공여부, 에러메시지, 저장건수, 스킵건수)
  """
  if not _is_configured():
    return False, "gcp_service_account secrets 미설정", 0, 0
  try:
    ws = _get_worksheet()

    # 기존 키 수집 (중복 방지)
    existing_keys: set[tuple[str, str, str]] = set()
    try:
      for er in ws.get_all_records():
        existing_keys.add((
          str(er.get("scan_date", "")),
          str(er.get("strategy", "")),
          str(er.get("ticker", "")).strip(),
        ))
    except Exception:
      pass  # 기존 키 조회 실패 시 중복 체크 없이 전체 저장

    rows: list[list[object]] = []
    skipped = 0

    for _, r in df.iterrows():
      ticker = str(r["ticker"]).strip()
      key    = (scan_date, strategy, ticker)
      if key in existing_keys:
        skipped += 1
        continue

      rows.append([
        scan_date,                          # scan_date
        strategy,                           # strategy
        ticker,                             # ticker
        str(r.get("name", "")),             # name
        float(r["buy_price"]),              # buy_price
        "",                                 # entry_price  (공란)
        float(r["take_profit"]),            # take_profit
        float(r["stop_loss"]),              # stop_loss
        float(r["risk_reward"]),            # risk_reward
        round(float(r["pullback_pct"]), 2), # pullback_pct
        str(r.get("btc_direction", "")),    # btc_direction
        "",                                 # result       (공란)
        "",                                 # profit_pct   (공란)
        "",                                 # hold_days    (공란)
        "",                                 # actual_buy   (공란)
      ])

    if rows:
      ws.append_rows(rows, value_input_option="USER_ENTERED")

    return True, "", len(rows), skipped

  except Exception as e:
    return False, str(e), 0, 0


def update_coin_results() -> tuple[int, str]:
  """
  result 컬럼이 비어 있는 행을 pybithumb으로 자동 판정해 Sheets 업데이트.
  반환: (업데이트된 행 수, 에러 메시지 — 없으면 "")
  """
  if not _is_configured():
    return 0, ""
  try:
    ws       = _get_worksheet()
    all_rows = ws.get_all_records()
    if not all_rows:
      return 0, ""

    updated = 0

    for i, row in enumerate(all_rows):
      # 이미 판정된 행 스킵 (ERROR도 재시도하지 않음)
      if row.get("result") not in ("", None):
        continue

      scan_date = str(row.get("scan_date", "")).strip()
      if not scan_date:
        continue

      # 코인은 스캔 당일 즉시 진입
      try:
        fmt = "%Y%m%d" if "-" not in scan_date else "%Y-%m-%d"
        entry_date = datetime.strptime(scan_date, fmt).strftime("%Y-%m-%d")
      except ValueError:
        continue

      strategy  = str(row.get("strategy", "day")).strip()
      max_hold  = DAY_MAX_HOLD if strategy == "day" else SWING_MAX_HOLD
      ticker    = str(row.get("ticker", "")).strip()

      try:
        take_profit = float(row.get("take_profit", 0))
        stop_loss   = float(row.get("stop_loss", 0))
      except (ValueError, TypeError):
        continue

      entry_price, result, profit_pct, hold_days = _calc_coin_result(
        ticker, entry_date, take_profit, stop_loss, max_hold
      )

      if result == "PENDING":
        continue

      sheet_row   = i + 2  # 헤더 1행 + 0-index 보정
      col_entry   = COLUMNS.index("entry_price") + 1
      col_result  = COLUMNS.index("result")      + 1
      col_profit  = COLUMNS.index("profit_pct")  + 1
      col_hold    = COLUMNS.index("hold_days")   + 1

      ws.update_cell(sheet_row, col_entry,  round(entry_price, 4) if entry_price > 0 else "")
      ws.update_cell(sheet_row, col_result,  result)
      ws.update_cell(sheet_row, col_profit,  profit_pct)
      ws.update_cell(sheet_row, col_hold,    hold_days)
      updated += 1

    return updated, ""

  except Exception as e:
    return 0, str(e)


@st.cache_data(ttl=300)
def load_coin_history() -> pd.DataFrame:
  """코인 전용 시트 전체 히스토리 로드 (5분 캐시)."""
  if not _is_configured():
    return pd.DataFrame()
  try:
    ws      = _get_worksheet()
    records = ws.get_all_records()
    return pd.DataFrame(records) if records else pd.DataFrame()
  except Exception:
    return pd.DataFrame()


def evaluate_coin_strategy(df_done: pd.DataFrame) -> dict[str, object]:
  """
  완료 거래 DataFrame(WIN/LOSS/EXPIRED 포함) → 0~100점 채점 및 판정 반환.
  기존 sheets.py evaluate_strategy와 동일 로직.

  반환 구조:
    score       : int
    verdict     : str  ("합격" / "경고" / "재검토")
    breakdown   : dict (항목별 value, score, max)
    weak_points : list[str]
    sample_size : int
  """
  total = len(df_done)
  df    = df_done.copy()
  df["profit_pct"] = pd.to_numeric(df["profit_pct"], errors="coerce").fillna(0)

  wins   = (df["result"] == "WIN").sum()
  losses = df[df["result"].isin(["LOSS", "EXPIRED"])]

  win_rate = wins / total * 100
  ev       = df["profit_pct"].mean()

  cum = (1 + df["profit_pct"] / 100).cumprod()
  mdd = ((cum - cum.cummax()) / cum.cummax()).min() * 100

  avg_win  = df[df["result"] == "WIN"]["profit_pct"].mean() if wins > 0 else 0.0
  avg_loss = abs(losses["profit_pct"].mean()) if len(losses) > 0 else 0.0
  pl_ratio = avg_win / avg_loss if avg_loss > 0 else 0.0

  # 항목별 점수 산정
  wr_score  = 30 if win_rate >= 70 else 20 if win_rate >= 60 else 10 if win_rate >= 50 else 0
  ev_score  = 30 if ev >= 1.0      else 20 if ev >= 0.0       else 10 if ev >= -1.0     else 0
  mdd_score = 20 if mdd >= -10     else 10 if mdd >= -20      else 0
  pl_score  = 20 if pl_ratio >= 1.5 else 10 if pl_ratio >= 1.0 else 0

  score   = wr_score + ev_score + mdd_score + pl_score
  verdict = "합격" if score >= 80 else "경고" if score >= 60 else "재검토"

  weak_points: list[str] = []
  if wr_score  < 30: weak_points.append(f"승률 70% 미달 (현재 {win_rate:.1f}%)")
  if ev_score  < 30: weak_points.append(f"기대값 +1% 미달 (현재 {ev:.2f}%)")
  if mdd_score < 20: weak_points.append(f"MDD -10% 초과 (현재 {mdd:.1f}%)")
  if pl_score  < 20: weak_points.append(f"손익비 1.5 미달 (현재 {pl_ratio:.2f})")

  return {
    "score":   score,
    "verdict": verdict,
    "breakdown": {
      "win_rate":       {"value": round(win_rate, 1), "score": wr_score,  "max": 30},
      "expected_value": {"value": round(ev, 2),       "score": ev_score,  "max": 30},
      "mdd":            {"value": round(mdd, 1),      "score": mdd_score, "max": 20},
      "pl_ratio":       {"value": round(pl_ratio, 2), "score": pl_score,  "max": 20},
    },
    "weak_points": weak_points,
    "sample_size": total,
  }


def is_coin_configured() -> bool:
  """외부에서 설정 여부를 확인하는 퍼블릭 래퍼"""
  return _is_configured()
