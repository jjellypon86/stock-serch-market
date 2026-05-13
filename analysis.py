from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import yaml

from utils import add_moving_averages, get_ohlcv

with open("config.yaml", encoding="utf-8") as _f:
    _CFG = yaml.safe_load(_f)

_CURRENT_WEIGHTS: dict[str, int] = _CFG["weights"]
_PULLBACK_BAND_PCT: float = _CFG["scanner"]["pullback_band"] * 100  # 0.03 → 3.0
_DAY_TP_MULT: float = _CFG["scanner"]["day"]["tp_mult"]
_DAY_SL_MULT: float = _CFG["scanner"]["day"]["sl_mult"]
_SWING_TP_MULT: float = _CFG["scanner"]["swing"]["tp_mult"]
_SWING_SL_MULT: float = _CFG["scanner"]["swing"]["sl_mult"]
_DAY_MAX_HOLD: int = _CFG["backtest"]["day_max_hold"]
_SWING_MAX_HOLD: int = _CFG["backtest"]["swing_max_hold"]


def _normalize_ticker(raw: object) -> str:
    try:
        return str(int(float(str(raw)))).zfill(6)
    except (ValueError, TypeError):
        return str(raw).strip()


def _next_trading_day(date_str: str) -> str:
    """YYYYMMDD → 다음 거래일 YYYYMMDD"""
    d = datetime.strptime(date_str, "%Y%m%d") + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y%m%d")


def prepare_done_df(df_hist: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """WIN/LOSS/EXPIRED 필터, 수치형 변환, 파생 컬럼 추가"""
    df = df_hist[df_hist["result"].isin(["WIN", "LOSS", "EXPIRED"])].copy()
    if len(df) < 10:
        return pd.DataFrame(), f"분석에 충분한 데이터가 없습니다 (완료 건수 {len(df)}건, 최소 10건 필요)"

    for col in ["profit_pct", "pullback_pct", "risk_reward", "hold_days", "inst_days", "foreign_days"]:
        df[col] = pd.to_numeric(df.get(col, pd.Series(0, index=df.index)), errors="coerce")

    df["supply_days"] = df["inst_days"].fillna(0) + df["foreign_days"].fillna(0)
    df["is_win"] = (df["result"] == "WIN").astype(int)
    return df.reset_index(drop=True), ""


def calc_overall_stats(df_done: pd.DataFrame) -> dict[str, Any]:
    """전체 성과 통계"""
    total = len(df_done)
    wins = int((df_done["result"] == "WIN").sum())
    losses = int((df_done["result"] == "LOSS").sum())
    expired = int((df_done["result"] == "EXPIRED").sum())

    win_rate = round(wins / total * 100, 1) if total > 0 else 0.0
    ev = round(float(df_done["profit_pct"].mean()), 2) if total > 0 else 0.0

    cum = (1 + df_done["profit_pct"] / 100).cumprod()
    mdd = round(float(((cum - cum.cummax()) / cum.cummax()).min() * 100), 1)

    avg_win = round(float(df_done[df_done["result"] == "WIN"]["profit_pct"].mean()), 2) if wins > 0 else 0.0
    avg_loss_val = float(
        abs(df_done[df_done["result"].isin(["LOSS", "EXPIRED"])]["profit_pct"].mean())
    ) if (losses + expired) > 0 else 0.0
    pl_ratio = round(avg_win / avg_loss_val, 2) if avg_loss_val > 0 else 0.0

    dates = pd.to_datetime(df_done["scan_date"].astype(str), format="%Y%m%d", errors="coerce").dropna()
    date_range = (
        f"{dates.min().strftime('%Y-%m-%d')} ~ {dates.max().strftime('%Y-%m-%d')}"
        if len(dates) > 0 else "-"
    )

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "expired": expired,
        "win_rate": win_rate,
        "expectancy": ev,
        "mdd": mdd,
        "pl_ratio": pl_ratio,
        "avg_win": avg_win,
        "date_range": date_range,
    }


def calc_group_comparison(df_done: pd.DataFrame) -> pd.DataFrame:
    """WIN vs LOSS 그룹별 지표 평균 비교"""
    win_df = df_done[df_done["result"] == "WIN"]
    loss_df = df_done[df_done["result"].isin(["LOSS", "EXPIRED"])]

    indicators: list[tuple[str, str]] = [
        ("pullback_pct", "눌림률(%)"),
        ("risk_reward",  "손익비"),
        ("supply_days",  "수급일수(기관+외국인)"),
        ("hold_days",    "보유일수"),
        ("profit_pct",   "수익률(%)"),
    ]

    rows = []
    for col, label in indicators:
        win_mean  = round(float(win_df[col].mean()), 2)  if len(win_df)  > 0 else float("nan")
        loss_mean = round(float(loss_df[col].mean()), 2) if len(loss_df) > 0 else float("nan")
        diff = round(win_mean - loss_mean, 2) if not (pd.isna(win_mean) or pd.isna(loss_mean)) else float("nan")

        if pd.isna(diff):
            direction = "데이터 없음"
        elif col == "pullback_pct":
            direction = "WIN이 더 얕은 눌림" if diff < -0.1 else "차이 미미"
        elif col == "hold_days":
            direction = "WIN이 더 빠른 청산" if diff < -0.5 else "LOSS가 더 빠른 청산" if diff > 0.5 else "차이 없음"
        else:
            direction = "WIN이 우세" if diff > 0.1 else "LOSS가 우세" if diff < -0.1 else "차이 없음"

        rows.append({
            "지표":      label,
            "WIN 평균":  win_mean,
            "LOSS 평균": loss_mean,
            "차이":      diff,
            "해석":      direction,
        })

    return pd.DataFrame(rows)


def calc_segment_stats(df_done: pd.DataFrame) -> pd.DataFrame:
    """strategy × market 조합별 성과"""
    rows = []
    for strategy in ["day", "swing"]:
        for market in ["KOSPI", "KOSDAQ"]:
            sub = df_done[(df_done["strategy"] == strategy) & (df_done["market"] == market)]
            if len(sub) < 5:
                continue
            wins = (sub["result"] == "WIN").sum()
            rows.append({
                "전략":      "단기" if strategy == "day" else "스윙",
                "시장":      market,
                "거래수":    len(sub),
                "승률(%)":   round(wins / len(sub) * 100, 1),
                "기대값(%)": round(float(sub["profit_pct"].mean()), 2),
                "평균WIN(%)": round(float(sub[sub["result"] == "WIN"]["profit_pct"].mean()), 2) if wins > 0 else 0.0,
            })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def calc_pullback_distribution(df_done: pd.DataFrame) -> dict[str, Any]:
    """pullback_pct 5구간별 WIN율 → pullback_band 권고"""
    series = df_done["pullback_pct"].dropna()
    if len(series) < 10:
        return {"table": pd.DataFrame(), "recommend_band": _PULLBACK_BAND_PCT, "current_band": _PULLBACK_BAND_PCT}

    try:
        bins = pd.cut(series, bins=5)
        result_series = df_done.loc[series.index, "result"]

        rows = []
        for i, interval in enumerate(bins.cat.categories):
            mask = bins == interval
            sub_result = result_series[mask]
            n = len(sub_result)
            wins = int((sub_result == "WIN").sum())
            rows.append({
                "구간":      str(interval),
                "거래수":    n,
                "WIN수":     wins,
                "WIN율(%)":  round(wins / n * 100, 1) if n >= 3 else None,
            })

        table = pd.DataFrame(rows)

        valid = table[table["WIN율(%)"].notna() & (table["거래수"] >= 3)]
        if len(valid) > 0:
            best_iloc = int(valid["WIN율(%)"].idxmax())
            interval_obj = bins.cat.categories[best_iloc]
            recommend_band = round(max(abs(float(interval_obj.left)), abs(float(interval_obj.right))), 1)
        else:
            recommend_band = _PULLBACK_BAND_PCT

        return {"table": table, "recommend_band": recommend_band, "current_band": _PULLBACK_BAND_PCT}
    except Exception:
        return {"table": pd.DataFrame(), "recommend_band": _PULLBACK_BAND_PCT, "current_band": _PULLBACK_BAND_PCT}


def calc_rr_threshold(df_done: pd.DataFrame) -> dict[str, Any]:
    """risk_reward 3구간별 WIN율 → tp_mult/sl_mult 권고"""
    series = df_done["risk_reward"].dropna()
    default = {
        "table": pd.DataFrame(),
        "rr_threshold": 2.0,
        "current_day_rr": round(_DAY_TP_MULT / _DAY_SL_MULT, 1),
        "current_swing_rr": round(_SWING_TP_MULT / _SWING_SL_MULT, 1),
    }
    if len(series) < 10:
        return default

    try:
        q33 = round(float(series.quantile(0.33)), 1)
        q67 = round(float(series.quantile(0.67)), 1)

        def _rr_group(rr: float) -> str:
            if rr <= q33:
                return f"낮음 (≤{q33})"
            elif rr <= q67:
                return f"중간 ({q33}~{q67})"
            else:
                return f"높음 (>{q67})"

        groups = series.apply(_rr_group)
        rows = []
        for label in [f"낮음 (≤{q33})", f"중간 ({q33}~{q67})", f"높음 (>{q67})"]:
            sub = df_done[groups == label]
            if len(sub) < 3:
                continue
            wins = (sub["result"] == "WIN").sum()
            rows.append({"구간": label, "거래수": len(sub), "WIN율(%)": round(wins / len(sub) * 100, 1)})

        return {
            "table": pd.DataFrame(rows) if rows else pd.DataFrame(),
            "rr_threshold": q67,
            "current_day_rr": round(_DAY_TP_MULT / _DAY_SL_MULT, 1),
            "current_swing_rr": round(_SWING_TP_MULT / _SWING_SL_MULT, 1),
        }
    except Exception:
        return default


def calc_supply_threshold(df_done: pd.DataFrame) -> dict[str, Any]:
    """supply_days 값별 WIN율 → 수급 필터 강화 권고"""
    rows = []
    for val in sorted(df_done["supply_days"].dropna().unique()):
        sub = df_done[df_done["supply_days"] == val]
        if len(sub) < 3:
            continue
        wins = (sub["result"] == "WIN").sum()
        rows.append({
            "수급일수 합계": int(val),
            "거래수":        len(sub),
            "WIN율(%)":      round(wins / len(sub) * 100, 1),
        })

    table = pd.DataFrame(rows) if rows else pd.DataFrame()

    recommend_min = 2
    if not table.empty:
        high_win = table[(table["WIN율(%)"] >= 55) & (table["수급일수 합계"] > 2)]
        if len(high_win) > 0:
            recommend_min = int(high_win["수급일수 합계"].min())

    return {"table": table, "recommend_min": recommend_min, "current_min": 2}


def calc_weight_suggestions(df_done: pd.DataFrame) -> dict[str, Any]:
    """지표별 Point-Biserial 상관계수 기반 가중치 재조정 제안"""
    is_win = df_done["is_win"]

    correlations: dict[str, float] = {}
    if "supply_days" in df_done.columns:
        correlations["supply"] = round(float(df_done["supply_days"].corr(is_win)), 3)
    if "risk_reward" in df_done.columns:
        correlations["risk_reward"] = round(float(df_done["risk_reward"].corr(is_win)), 3)
    if "pullback_pct" in df_done.columns:
        correlations["pullback"] = round(float((-df_done["pullback_pct"].abs()).corr(is_win)), 3)
    if "hold_days" in df_done.columns:
        correlations["trend_quality"] = round(float((-df_done["hold_days"]).corr(is_win)), 3)

    corr_abs = {k: abs(v) for k, v in correlations.items() if not pd.isna(v)}
    total_corr = sum(corr_abs.values())

    if total_corr > 0:
        raw = {k: v / total_corr * 90 for k, v in corr_abs.items()}
        suggested: dict[str, int] = {k: round(v) for k, v in raw.items()}
        diff = 90 - sum(suggested.values())
        if diff != 0 and suggested:
            max_key = max(suggested, key=lambda k: corr_abs.get(k, 0))
            suggested[max_key] += diff
        for key in _CURRENT_WEIGHTS:
            if key not in suggested:
                suggested[key] = _CURRENT_WEIGHTS[key]
        suggested["rsi_score"] = 10
    else:
        suggested = dict(_CURRENT_WEIGHTS)

    top_predictor = max(correlations, key=lambda k: abs(correlations[k])) if correlations else "supply"

    return {
        "correlations":     correlations,
        "current_weights":  dict(_CURRENT_WEIGHTS),
        "suggested_weights": suggested,
        "top_predictor":    top_predictor,
        "reliable":         len(df_done) >= 30,
    }


def classify_failure_reason(row: pd.Series) -> dict[str, Any]:
    """LOSS/EXPIRED 종목의 실패 원인을 OHLCV 기반으로 분류"""
    ticker   = _normalize_ticker(row.get("ticker", ""))
    name     = str(row.get("name", ""))
    result   = str(row.get("result", ""))
    scan_date = str(row.get("scan_date", ""))

    base = {"ticker": ticker, "name": name, "result": result,
            "primary_reason": "", "suggestion": "", "reached_pct": None}

    try:
        entry_raw = row.get("entry_price") or row.get("buy_price", 0)
        entry_price = float(str(entry_raw).replace(",", "")) if entry_raw else 0.0
        take_profit = float(str(row.get("take_profit", 0)).replace(",", ""))
        stop_loss   = float(str(row.get("stop_loss", 0)).replace(",", ""))
        strategy    = str(row.get("strategy", "day"))
        max_hold    = _DAY_MAX_HOLD if strategy == "day" else _SWING_MAX_HOLD
    except (ValueError, TypeError):
        return {**base, "primary_reason": "데이터 파싱 오류"}

    try:
        dt = datetime.strptime(scan_date, "%Y%m%d")
        start = (dt - timedelta(days=90)).strftime("%Y%m%d")
        end   = (dt + timedelta(days=max_hold + 15)).strftime("%Y%m%d")
        df = get_ohlcv(ticker, start, end)
        if df.empty:
            return {**base, "primary_reason": "OHLCV 데이터 조회 실패"}
        df = add_moving_averages(df)
    except Exception:
        return {**base, "primary_reason": "OHLCV 데이터 조회 실패"}

    # 진입일(scan_date 다음 거래일) 이후 데이터 추출
    try:
        entry_date_str = _next_trading_day(scan_date)
        entry_ts = pd.Timestamp(datetime.strptime(entry_date_str, "%Y%m%d"))
        entry_iloc = df.index.searchsorted(entry_ts)
        if entry_iloc >= len(df):
            return {**base, "primary_reason": "진입일 이후 데이터 없음"}
        holding_df = df.iloc[entry_iloc: entry_iloc + max_hold + 1]
    except Exception:
        return {**base, "primary_reason": "진입일 탐색 실패"}

    primary_reason = ""
    suggestion     = ""
    reached_pct    = None

    if result == "LOSS":
        # 추세 역전: 보유 기간 중 MA20이 한 번이라도 하락했는가
        ma20 = holding_df["MA20"].dropna()
        ma20_declining = bool((ma20.diff().dropna() < 0).any()) if len(ma20) >= 2 else False

        # ATR 과소평가: 일중 평균 변동폭 > 손절폭 × 1.3
        intraday_range = float((holding_df["고가"] - holding_df["저가"]).mean())
        stop_width = entry_price - stop_loss if entry_price > stop_loss > 0 else 0.0
        atr_underestimated = stop_width > 0 and intraday_range > stop_width * 1.3

        # 수급 약세
        supply_days = (row.get("inst_days") or 0) + (row.get("foreign_days") or 0)
        weak_supply = supply_days <= 2

        if ma20_declining:
            primary_reason = "추세 역전 — MA20이 진입 후 하락 전환, 지지 붕괴"
            suggestion     = "진입 전 MA20 기울기 강도 조건 추가 검토"
        elif atr_underestimated:
            primary_reason = (
                f"ATR 과소평가 — 실제 일중변동폭({intraday_range:.0f}원)이 "
                f"손절폭({stop_width:.0f}원)보다 큼"
            )
            suggestion = "sl_mult 상향 또는 변동성 높은 시기 진입 자제"
        elif weak_supply:
            primary_reason = "수급 약세 — 기관/외국인 순매수 강도가 필터 경계치"
            suggestion     = "수급 필터 강화 (supply_days >= 3 조건 검토)"
        else:
            primary_reason = "복합 요인 — 명확한 단일 원인 없음"
            suggestion     = "시장 전체 흐름 점검 (KOSPI 방향 필터 강화 검토)"

    elif result == "EXPIRED":
        if len(holding_df) > 0 and entry_price > 0 and take_profit > entry_price:
            max_high = float(holding_df["고가"].max())
            reached_pct = round((max_high - entry_price) / (take_profit - entry_price) * 100, 1)

            tp_mult = _DAY_TP_MULT if strategy == "day" else _SWING_TP_MULT
            if reached_pct < 30:
                primary_reason = f"모멘텀 부재 — 익절가 방향 움직임 {reached_pct:.0f}% (횡보/하락)"
                suggestion     = "진입 시점 거래량 회복 신호 조건 강화 검토"
            elif reached_pct < 70:
                primary_reason = f"목표가 과다 설정 — 최고점이 익절가의 {reached_pct:.0f}%까지만 도달"
                suggestion     = f"tp_mult {tp_mult} → 소폭 축소 검토"
            else:
                primary_reason = f"아쉬운 이탈 — 익절가 {reached_pct:.0f}% 접근 후 반락"
                suggestion     = "tp_mult 소폭 축소 또는 트레일링 스톱 적용 검토"
        else:
            primary_reason = "만기 청산 — 보유 기간 내 익절/손절 미달"
            suggestion     = "max_hold 조정 또는 tp_mult 축소 검토"

    return {**base, "primary_reason": primary_reason, "suggestion": suggestion, "reached_pct": reached_pct}


def generate_recommendations(
    pullback_result: dict[str, Any],
    rr_result:       dict[str, Any],
    supply_result:   dict[str, Any],
    weight_result:   dict[str, Any],
    overall_stats:   dict[str, Any],
) -> list[dict[str, Any]]:
    """분석 결과 취합 → 최대 3개 핵심 권고사항"""
    candidates: list[dict[str, Any]] = []

    # pullback_band 조정
    c_band = pullback_result.get("current_band", 3.0)
    r_band = pullback_result.get("recommend_band", 3.0)
    if abs(c_band - r_band) >= 0.5:
        direction = "축소" if r_band < c_band else "확대"
        candidates.append({
            "priority": 0,
            "param":    "pullback_band",
            "current":  f"{c_band:.1f}%",
            "suggested": f"{r_band:.1f}%",
            "reason":   f"WIN율 최고 구간 기준 {direction} 권고",
            "effect":   "신호 수 변화 있으나 승률 향상 기대",
            "impact":   abs(c_band - r_band),
        })

    # 수급 필터 강화
    rec_min = supply_result.get("recommend_min", 2)
    cur_min = supply_result.get("current_min", 2)
    if rec_min > cur_min:
        candidates.append({
            "priority": 0,
            "param":    "수급 필터",
            "current":  f"합계 >= {cur_min}",
            "suggested": f"합계 >= {rec_min}",
            "reason":   f"supply_days >= {rec_min} 구간에서 WIN율 55% 이상",
            "effect":   "신호 수 감소, 정확도 향상",
            "impact":   float(rec_min - cur_min),
        })

    # 가중치 재조정 (30건 이상 데이터에서만)
    top    = weight_result.get("top_predictor", "")
    curr_w = weight_result.get("current_weights", {})
    sugg_w = weight_result.get("suggested_weights", {})
    if top and top in curr_w and top in sugg_w and weight_result.get("reliable", False):
        diff_w = sugg_w[top] - curr_w[top]
        if abs(diff_w) >= 5:
            candidates.append({
                "priority": 0,
                "param":    "가중치 재조정",
                "current":  f"{top} {curr_w[top]}%",
                "suggested": f"{top} {sugg_w[top]}% (데이터 기반)",
                "reason":   f"{top}가 WIN/LOSS 예측력 최고",
                "effect":   "스코어링 정확도 개선 기대",
                "impact":   float(abs(diff_w)),
            })

    # 기대값 음수면 경고 추가
    if overall_stats.get("expectancy", 0) < 0 and len(candidates) < 3:
        candidates.append({
            "priority": 0,
            "param":    "전략 전반 재검토",
            "current":  f"기대값 {overall_stats['expectancy']:.2f}%",
            "suggested": "필터 조건 강화 또는 백테스트 재검증",
            "reason":   "기대값 음수 — 장기 손실 구조",
            "effect":   "신호 수 감소하나 수익성 개선 가능",
            "impact":   abs(float(overall_stats.get("expectancy", 0))),
        })

    candidates.sort(key=lambda x: x["impact"], reverse=True)
    for i, c in enumerate(candidates[:3]):
        c["priority"] = i + 1

    return candidates[:3]


def run_full_analysis(df_hist: pd.DataFrame) -> tuple[dict[str, Any], str]:
    """전체 분석 파이프라인 실행. 반환: (report_dict, error_msg)"""
    df_done, err = prepare_done_df(df_hist)
    if err:
        return {}, err

    overall_stats    = calc_overall_stats(df_done)
    group_comparison = calc_group_comparison(df_done)
    segment_stats    = calc_segment_stats(df_done)
    pullback_result  = calc_pullback_distribution(df_done)
    rr_result        = calc_rr_threshold(df_done)
    supply_result    = calc_supply_threshold(df_done)
    weight_result    = calc_weight_suggestions(df_done)
    recommendations  = generate_recommendations(
        pullback_result, rr_result, supply_result, weight_result, overall_stats
    )

    failure_analysis: list[dict[str, Any]] = []
    for _, row in df_done[df_done["result"].isin(["LOSS", "EXPIRED"])].iterrows():
        failure_analysis.append(classify_failure_reason(row))

    return {
        "analysis_date":   datetime.now().strftime("%Y-%m-%d"),
        "overall_stats":   overall_stats,
        "group_comparison": group_comparison,
        "segment_stats":   segment_stats,
        "pullback_result": pullback_result,
        "rr_result":       rr_result,
        "supply_result":   supply_result,
        "weight_result":   weight_result,
        "failure_analysis": failure_analysis,
        "recommendations": recommendations,
    }, ""
