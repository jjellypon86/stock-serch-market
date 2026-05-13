from __future__ import annotations

from datetime import datetime, timedelta

import FinanceDataReader as fdr
import pandas as pd
import streamlit as st
import yaml

with open("config.yaml") as _f:
    _scfg = yaml.safe_load(_f)

try:
    from google.oauth2.service_account import Credentials
    import gspread
    _GSPREAD_OK = True
except ImportError:
    _GSPREAD_OK = False

SHEET_NAME = "K-Quant Tracker"
HIST_WS    = "history"
SCOPES     = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

COLUMNS = [
    "scan_date", "strategy", "market", "ticker", "name",
    "buy_price", "entry_price",
    "take_profit", "stop_loss", "risk_reward", "pullback_pct",
    "inst_days", "foreign_days",
    "result", "profit_pct", "hold_days",
    "actual_buy",
    "strategy_ver",
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


def save_scan_results(df: pd.DataFrame, strategy: str, market: str, scan_date: str) -> tuple[bool, str, int, int]:
    """스캔 완료 즉시 Sheets에 추가. 결과 컬럼은 공란.
    반환: (성공여부, 에러메시지, 저장건수, 중복건너뜀건수)"""
    if not _is_configured():
        return False, "gcp_service_account secrets 미설정", 0, 0
    try:
        ws = _get_worksheet()

        existing_keys: set[tuple[str, str, str]] = set()
        try:
            for er in ws.get_all_records():
                raw = str(er.get("ticker", ""))
                try:
                    t = str(int(float(raw))).zfill(6)
                except (ValueError, TypeError):
                    t = raw.strip()
                existing_keys.add((
                    str(er.get("scan_date", "")),
                    str(er.get("strategy", "")),
                    t,
                ))
        except Exception:
            pass  # 기존 키 조회 실패 시 중복 체크 없이 전체 저장

        rows = []
        skipped = 0
        for _, r in df.iterrows():
            key = (scan_date, strategy, str(r["ticker"]).zfill(6))
            if key in existing_keys:
                skipped += 1
                continue
            rows.append([
                scan_date, strategy, market,
                "'" + str(r["ticker"]).zfill(6), r["name"],
                int(r["buy_price"]), "",
                int(r["take_profit"]), int(r["stop_loss"]),
                float(r["risk_reward"]), round(float(r["pullback_pct"]), 2),
                int(r.get("inst_days", 0)), int(r.get("foreign_days", 0)),
                "", "", "",
                "",
                _scfg["strategy"]["version"],
            ])

        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
        return True, "", len(rows), skipped
    except Exception as e:
        return False, str(e), 0, 0


def _next_trading_day(date_str: str) -> str:
    """날짜 문자열(YYYYMMDD 또는 YYYY-MM-DD)의 다음 거래일 반환"""
    fmt = "%Y%m%d" if "-" not in date_str else "%Y-%m-%d"
    d = datetime.strptime(date_str, fmt) + timedelta(days=1)
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


def update_results() -> tuple[int, str]:
    """
    result 컬럼이 비어 있는 행을 FDR로 자동 판정해 Sheets 업데이트.
    반환: (업데이트된 행 수, 에러 메시지 — 없으면 "")
    """
    if not _is_configured():
        return 0, ""
    try:
        ws = _get_worksheet()
        all_rows = ws.get_all_records()
        if not all_rows:
            return 0, ""

        updated = 0
        for i, row in enumerate(all_rows):
            if row.get("result") not in ("", None, "ERROR"):
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
            # --- 여기서부터 수정 ---
            raw_ticker = row.get("ticker", "")
            try:
                # 5930.0 같은 float 형태나 5930 같은 int 형태를 모두 '005930'으로 변환
                ticker = str(int(float(raw_ticker))).zfill(6)
            except (ValueError, TypeError):
                # 데이터가 비어있거나 변환 불가능한 문자열일 경우 대비
                ticker = str(raw_ticker).strip().zfill(6)
            # --- 여기까지 수정 ---
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

        return updated, ""
    except Exception as e:
        return 0, str(e)


@st.cache_data(ttl=300)
def load_history() -> pd.DataFrame:
    """Sheets 전체 히스토리 로드 (5분 캐시)."""
    if not _is_configured():
        return pd.DataFrame()
    try:
        ws = _get_worksheet()
        records = ws.get_all_records()
        return pd.DataFrame(records) if records else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def evaluate_strategy(df_done: pd.DataFrame) -> dict:
    """완료 거래 DataFrame(30건+) → 0~100점 채점 및 판정 반환"""
    total = len(df_done)
    df = df_done.copy()
    df["profit_pct"] = pd.to_numeric(df["profit_pct"], errors="coerce").fillna(0)

    wins   = (df["result"] == "WIN").sum()
    losses = df[df["result"].isin(["LOSS", "EXPIRED"])]

    win_rate = wins / total * 100
    ev       = df["profit_pct"].mean()

    cum   = (1 + df["profit_pct"] / 100).cumprod()
    mdd   = ((cum - cum.cummax()) / cum.cummax()).min() * 100

    avg_win  = df[df["result"] == "WIN"]["profit_pct"].mean() if wins > 0 else 0.0
    avg_loss = abs(losses["profit_pct"].mean()) if len(losses) > 0 else 0.0
    pl_ratio = avg_win / avg_loss if avg_loss > 0 else 0.0

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
        "score": score,
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


def _safe_val(v: object) -> object:
    """NaN/Inf → 빈 문자열 변환 (gspread JSON 직렬화 오류 방지)"""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return v


def _safe_rows(rows: list[list]) -> list[list]:
    return [[_safe_val(cell) for cell in row] for row in rows]


def save_analysis_report(report: dict) -> tuple[str, str]:
    """분석 리포트를 Sheets 새 탭에 저장. 반환: (탭 이름, 에러 메시지)"""
    if not _is_configured():
        return "", "gcp_service_account secrets 미설정"

    try:
        creds = Credentials.from_service_account_info(
            st.secrets["gcp_service_account"], scopes=SCOPES
        )
        gc = gspread.authorize(creds)
        sh = gc.open(SHEET_NAME)

        tab_name = f"분석_{report['analysis_date'].replace('-', '')}"

        # 동일 날짜 탭 이미 있으면 삭제 후 재생성
        try:
            sh.del_worksheet(sh.worksheet(tab_name))
        except gspread.WorksheetNotFound:
            pass

        ws = sh.add_worksheet(title=tab_name, rows=300, cols=10)

        stats = report["overall_stats"]
        rows: list[list] = []

        # 메타데이터
        rows += [
            ["▶ 분석 메타데이터"],
            ["분석일",      report["analysis_date"]],
            ["데이터 기간", stats.get("date_range", "-")],
            ["분석 대상",
             f"WIN {stats['wins']}건 / LOSS {stats['losses']}건 / "
             f"EXPIRED {stats['expired']}건 / 합계 {stats['total']}건"],
            [],
        ]

        # 1. 전체 성과 통계
        rows += [
            ["▶ 1. 전체 성과 통계"],
            ["승률(%)", "기대값(%)", "MDD(%)", "손익비"],
            [stats["win_rate"], stats["expectancy"], stats["mdd"], stats["pl_ratio"]],
            [],
        ]

        # 2. WIN vs LOSS 지표 비교
        rows.append(["▶ 2. WIN vs LOSS 지표 비교"])
        cmp_df = report.get("group_comparison")
        if cmp_df is not None and not cmp_df.empty:
            rows.append(list(cmp_df.columns))
            for _, r in cmp_df.iterrows():
                rows.append([str(v) for v in r.tolist()])
        rows.append([])

        # 3. 전략×시장별 성과
        rows.append(["▶ 3. 전략×시장별 성과"])
        seg_df = report.get("segment_stats")
        if seg_df is not None and not seg_df.empty:
            rows.append(list(seg_df.columns))
            for _, r in seg_df.iterrows():
                rows.append([str(v) for v in r.tolist()])
        rows.append([])

        # 4. 파라미터 조정 권고
        rows.append(["▶ 4. 파라미터 조정 권고"])
        rows.append(["항목", "현재값", "권고값", "근거"])
        pb = report.get("pullback_result", {})
        if abs(pb.get("recommend_band", 3.0) - pb.get("current_band", 3.0)) >= 0.5:
            rows.append([
                "pullback_band",
                f"{pb['current_band']}%",
                f"{pb['recommend_band']}%",
                "WIN율 최고 구간 기준",
            ])
        sup = report.get("supply_result", {})
        if sup.get("recommend_min", 2) > sup.get("current_min", 2):
            rows.append([
                "수급 필터",
                f"합계 >= {sup['current_min']}",
                f"합계 >= {sup['recommend_min']}",
                "WIN율 55% 이상 구간",
            ])
        rows.append([])

        # 5. 가중치 재조정 제안
        rows.append(["▶ 5. 가중치 재조정 제안"])
        rows.append(["지표", "현재 가중치", "제안 가중치", "상관계수"])
        wt = report.get("weight_result", {})
        for key in wt.get("current_weights", {}):
            rows.append([
                key,
                wt["current_weights"].get(key, "-"),
                wt.get("suggested_weights", {}).get(key, "-"),
                wt.get("correlations", {}).get(key, "-"),
            ])
        rows.append([])

        # 6. 종목별 실패 원인 분석
        rows.append(["▶ 6. 종목별 실패 원인 분석"])
        rows.append(["종목코드", "종목명", "결과", "1차 원인", "개선 제안"])
        for fa in report.get("failure_analysis", []):
            rows.append([
                fa.get("ticker", ""),
                fa.get("name", ""),
                fa.get("result", ""),
                fa.get("primary_reason", ""),
                fa.get("suggestion", ""),
            ])
        rows.append([])

        # 7. 핵심 권고사항
        rows.append(["▶ 7. 핵심 권고사항"])
        rows.append(["우선순위", "파라미터", "현재값", "권고값", "예상 효과"])
        for rec in report.get("recommendations", []):
            rows.append([
                rec.get("priority", ""),
                rec.get("param", ""),
                rec.get("current", ""),
                rec.get("suggested", ""),
                rec.get("effect", ""),
            ])

        ws.append_rows(_safe_rows(rows), value_input_option="USER_ENTERED")
        return tab_name, ""

    except Exception as e:
        return "", str(e)


def is_configured() -> bool:
    return _is_configured()
