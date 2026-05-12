from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

try:
  from coin_scanner import (
    scan_coin_day,
    scan_coin_day_debug,
    scan_coin_swing,
    scan_coin_swing_debug,
  )
  _SCANNER_OK = True
except ImportError:
  _SCANNER_OK = False

from coin_sheets import (
  evaluate_coin_strategy,
  is_coin_configured,
  load_coin_history,
  save_coin_scan_results,
  update_coin_results,
)
from coin_utils import get_btc_direction

st.set_page_config(
  page_title="Coin Quant Tracker",
  page_icon="🪙",
  layout="centered",
)

st.title("🪙 Coin Quant Tracker")
st.caption("빗썸 KRW 코인 MA 눌림목 스캐너 — 수동매매 신호 생성")

# ── session_state 초기화 ──────────────────────────────────────────────────────

if "df_coin_day" not in st.session_state:
  st.session_state.df_coin_day = pd.DataFrame()
if "df_coin_swing" not in st.session_state:
  st.session_state.df_coin_swing = pd.DataFrame()
if "last_scan_day_time" not in st.session_state:
  st.session_state.last_scan_day_time = None
if "last_scan_swing_time" not in st.session_state:
  st.session_state.last_scan_swing_time = None
if "debug_day" not in st.session_state:
  st.session_state.debug_day: dict[str, int] = {}
if "debug_swing" not in st.session_state:
  st.session_state.debug_swing: dict[str, int] = {}

# ── 탭 구성 ──────────────────────────────────────────────────────────────────

tab_day, tab_swing, tab_verify = st.tabs([
  "📊 코인 단기 (MA20)", "📅 코인 스윙 (MA60)", "📈 코인 검증"
])


# ── BTC 방향 배너 ─────────────────────────────────────────────────────────────

def render_btc_banner() -> str:
  """BTC 시장 방향에 따라 스캔 탭 상단에 상태 배너 표시. 방향 문자열 반환."""
  direction = get_btc_direction()
  if direction == "상승":
    st.success("🟢 BTC 상승 추세 — 스캔 정상 실행")
  elif direction == "하락":
    st.error("🔴 BTC 하락 추세 — BTC MA60 이하, 신호 신뢰도 낮음")
  else:
    st.warning("🟡 BTC 중립 — BTC MA20~MA60 사이, 선택적 진입")
  return direction


def render_scan_time(last_time: datetime | None) -> None:
  """마지막 스캔 시간과 오늘 스캔 완료 여부 표시."""
  if last_time is None:
    st.caption("⚠️ 아직 스캔 전 — 매일 오전 9시 이후 스캔 권장")
    return
  time_str = last_time.strftime("%Y-%m-%d %H:%M")
  today = datetime.now().date()
  if last_time.date() == today:
    st.caption(f"✅ 오늘 스캔 완료 — 마지막 스캔: {time_str}")
  else:
    st.caption(f"⚠️ 오늘 스캔 전 — 마지막 스캔: {time_str}")


def render_filter_debug(debug: dict[str, int]) -> None:
  """필터 진단 expander: 각 필터 통과 코인 수 테이블로 표시."""
  with st.expander("🔍 필터 진단 — 어느 단계에서 코인이 탈락하는가"):
    if not debug:
      st.info("스캔을 실행하면 필터 진단 결과가 여기에 표시됩니다.")
      return
    if "BTC방향_차단" in debug:
      st.error("BTC 방향 조건 미충족으로 스캔이 실행되지 않았습니다.")
      return
    rows = [{"필터 단계": k, "통과 코인 수": v} for k, v in debug.items()]
    df_debug = pd.DataFrame(rows)
    st.dataframe(df_debug, use_container_width=True, hide_index=True)
    total = debug.get("0_입력", 0)
    final = debug.get("7_RSI", 0)
    if total > 0:
      st.caption(f"전체 {total}개 코인 중 최종 {final}개 통과 ({final/total*100:.1f}%)")


# ── 코인 카드 ─────────────────────────────────────────────────────────────────

def render_coin_card(row: pd.Series) -> None:
  """코인 단건 상세 카드 렌더링."""
  ticker = str(row["ticker"])
  close = float(row["close"])
  tp = float(row["take_profit"])
  sl = float(row["stop_loss"])
  rr = float(row["risk_reward"])
  pullback_pct = float(row["pullback_pct"])
  rsi = float(row.get("rsi", 0) or 0)

  label = (
    f"**{ticker}**"
    f"  —  눌림 {pullback_pct:+.1f}%"
    f"  /  손익비 {rr}:1"
    f"  /  RSI {rsi:.0f}"
  )
  with st.expander(label, expanded=True):
    r1c1, r1c2 = st.columns(2)
    r1c1.metric(
      "매수 참고가 💰",
      f"{close:,.0f}원",
      delta="현재 종가 기준",
      delta_color="off",
    )
    r1c2.metric(
      "RSI",
      f"{rsi:.1f}",
      delta="75 이하 통과",
      delta_color="off",
    )

    r2c1, r2c2 = st.columns(2)
    tp_pct = round((tp / close - 1) * 100, 1)
    sl_pct = round((sl / close - 1) * 100, 1)
    r2c1.metric(
      "익절가 🎯",
      f"{tp:,.0f}원",
      delta=f"+{tp_pct}%",
    )
    r2c2.metric(
      "손절가 🛑",
      f"{sl:,.0f}원",
      delta=f"{sl_pct}%",
      delta_color="inverse",
    )

    st.divider()

    col_a, col_b = st.columns(2)
    col_a.info(f"📊 눌림폭: **{pullback_pct:+.2f}%**")
    col_b.info(f"⚡ ATR: **{float(row.get('atr', 0) or 0):,.4f}**")

    # 매매 가이드
    with st.expander("📋 매매 가이드"):
      st.markdown(f"""
- **진입 참고가**: {close:,.0f}원 (현재 종가 기준, 직접 시장가 주문)
- **익절 목표**: {tp:,.0f}원 (현재가 대비 **+{tp_pct}%**)
- **손절 기준**: {sl:,.0f}원 (현재가 대비 **{sl_pct}%**)
- **손익비**: {rr}:1
- ⚠️ 빗썸 앱에서 수동으로 주문 — 이 신호는 참고용입니다
""")


# ── 탭 1: 코인 단기 ──────────────────────────────────────────────────────────

with tab_day:
  render_btc_banner()
  st.subheader("MA20 눌림목 스캔")

  render_scan_time(st.session_state.last_scan_day_time)

  allow_neutral_day = st.checkbox(
    "BTC 중립 시장도 허용 (신호 증가, 리스크 상승)",
    key="allow_neutral_day",
  )

  if not _SCANNER_OK:
    st.error("coin_scanner.py를 찾을 수 없습니다. 모듈 생성 후 재실행해 주세요.")
  else:
    if st.button("🔍 단기 스캔 실행", key="scan_day"):
      with st.spinner("코인 스캔 중..."):
        st.session_state.df_coin_day = scan_coin_day(allow_neutral=allow_neutral_day)
        st.session_state.last_scan_day_time = datetime.now()
        st.session_state.debug_day = scan_coin_day_debug(allow_neutral=allow_neutral_day)

  df_day = st.session_state.df_coin_day

  if df_day.empty:
    st.info("스캔을 실행하거나, BTC 방향 조건 미충족으로 신호 없음")
  else:
    st.success(f"✅ 상위 {len(df_day)}개 코인 발견")
    for _, row in df_day.iterrows():
      render_coin_card(row)

    st.divider()

    # Sheets 저장 버튼
    if is_coin_configured():
      scan_date_str = datetime.now().strftime("%Y-%m-%d")
      if st.button("💾 Sheets에 저장", key="save_day"):
        ok, err, saved, skipped = save_coin_scan_results(df_day, "day", scan_date_str)
        if ok:
          st.success(f"저장 완료 — {saved}건 저장, {skipped}건 중복 스킵")
        else:
          st.error(f"저장 실패: {err}")
    else:
      st.caption("⚙️ gcp_service_account secrets 미설정 — Sheets 저장 불가")

    # 상세 테이블
    with st.expander("📋 전체 스캔 결과"):
      display_cols = ["ticker", "close", "take_profit", "stop_loss", "risk_reward", "pullback_pct", "rsi", "volume_24h"]
      available = [c for c in display_cols if c in df_day.columns]
      st.dataframe(df_day[available], use_container_width=True)

  render_filter_debug(st.session_state.debug_day)


# ── 탭 2: 코인 스윙 ──────────────────────────────────────────────────────────

with tab_swing:
  render_btc_banner()
  st.subheader("MA60 눌림목 스캔")

  render_scan_time(st.session_state.last_scan_swing_time)

  allow_neutral_swing = st.checkbox(
    "BTC 중립 시장도 허용 (신호 증가, 리스크 상승)",
    key="allow_neutral_swing",
  )

  if not _SCANNER_OK:
    st.error("coin_scanner.py를 찾을 수 없습니다. 모듈 생성 후 재실행해 주세요.")
  else:
    if st.button("🔍 스윙 스캔 실행", key="scan_swing"):
      with st.spinner("코인 스캔 중..."):
        st.session_state.df_coin_swing = scan_coin_swing(allow_neutral=allow_neutral_swing)
        st.session_state.last_scan_swing_time = datetime.now()
        st.session_state.debug_swing = scan_coin_swing_debug(allow_neutral=allow_neutral_swing)

  df_swing = st.session_state.df_coin_swing

  if df_swing.empty:
    st.info("스캔을 실행하거나, BTC 방향 조건 미충족으로 신호 없음")
  else:
    st.success(f"✅ 상위 {len(df_swing)}개 코인 발견")
    for _, row in df_swing.iterrows():
      render_coin_card(row)

    st.divider()

    # Sheets 저장 버튼
    if is_coin_configured():
      scan_date_str = datetime.now().strftime("%Y-%m-%d")
      if st.button("💾 Sheets에 저장", key="save_swing"):
        ok, err, saved, skipped = save_coin_scan_results(df_swing, "swing", scan_date_str)
        if ok:
          st.success(f"저장 완료 — {saved}건 저장, {skipped}건 중복 스킵")
        else:
          st.error(f"저장 실패: {err}")
    else:
      st.caption("⚙️ gcp_service_account secrets 미설정 — Sheets 저장 불가")

    # 상세 테이블
    with st.expander("📋 전체 스캔 결과"):
      display_cols = ["ticker", "close", "take_profit", "stop_loss", "risk_reward", "pullback_pct", "rsi", "volume_24h"]
      available = [c for c in display_cols if c in df_swing.columns]
      st.dataframe(df_swing[available], use_container_width=True)

  render_filter_debug(st.session_state.debug_swing)


# ── 탭 3: 코인 검증 ──────────────────────────────────────────────────────────

with tab_verify:
  st.subheader("코인 매매 검증")

  if not is_coin_configured():
    st.warning("gcp_service_account secrets 미설정 — Sheets 연동 불가")
  else:
    # 결과 자동 판정 버튼
    if st.button("🔄 결과 자동 판정 (PENDING → WIN/LOSS/EXPIRED)"):
      with st.spinner("판정 중..."):
        updated, err = update_coin_results()
      if err:
        st.error(f"오류: {err}")
      else:
        st.success(f"{updated}건 업데이트 완료")
        st.cache_data.clear()

    # 히스토리 로드
    df_hist = load_coin_history()

    if df_hist.empty:
      st.info("저장된 코인 매매 데이터 없음")
    else:
      # 완료/대기 건수 분류
      df_done = df_hist[df_hist["result"].isin(["WIN", "LOSS", "EXPIRED"])].copy()
      df_pending = df_hist[df_hist["result"].apply(
        lambda x: str(x).strip() in ("", "PENDING", "None")
      )]

      col1, col2, col3, col4 = st.columns(4)
      col1.metric("총 신호", len(df_hist))
      col2.metric("완료", len(df_done))
      col3.metric("대기중", len(df_pending))

      if len(df_done) > 0:
        df_done_typed = df_done.copy()
        df_done_typed["profit_pct"] = pd.to_numeric(
          df_done_typed["profit_pct"], errors="coerce"
        ).fillna(0)

        wins = (df_done_typed["result"] == "WIN").sum()
        win_rate = wins / len(df_done_typed) * 100
        ev = df_done_typed["profit_pct"].mean()
        col4.metric("승률", f"{win_rate:.1f}%")

        c1, c2 = st.columns(2)
        c1.metric("기대값 (per trade)", f"{ev:+.2f}%")

        # 30건 이상 시 전략 점수 표시
        if len(df_done) >= 30:
          st.divider()
          st.subheader("📊 전략 자가 진단")
          result = evaluate_coin_strategy(df_done_typed)
          score = int(result["score"])
          verdict = str(result["verdict"])
          verdict_color = "🟢" if verdict == "합격" else "🟡" if verdict == "경고" else "🔴"
          st.metric("전략 점수", f"{score}/100", delta=f"{verdict_color} {verdict}")

          bd = result["breakdown"]
          b1, b2, b3, b4 = st.columns(4)
          b1.metric(
            "승률 점수",
            f"{bd['win_rate']['score']}/{bd['win_rate']['max']}",
            delta=f"{bd['win_rate']['value']}%",
          )
          b2.metric(
            "기대값 점수",
            f"{bd['expected_value']['score']}/{bd['expected_value']['max']}",
            delta=f"{bd['expected_value']['value']}%",
          )
          b3.metric(
            "MDD 점수",
            f"{bd['mdd']['score']}/{bd['mdd']['max']}",
            delta=f"{bd['mdd']['value']}%",
          )
          b4.metric(
            "손익비 점수",
            f"{bd['pl_ratio']['score']}/{bd['pl_ratio']['max']}",
            delta=str(bd['pl_ratio']['value']),
          )

          weak_points = result.get("weak_points", [])
          if isinstance(weak_points, list) and weak_points:
            st.warning("개선 필요: " + " / ".join(weak_points))
        else:
          st.info(f"전략 점수는 완료 30건 이상 필요 (현재 {len(df_done)}건)")

      st.divider()
      st.subheader("📋 히스토리")

      # 필터 컨트롤
      fc1, fc2, fc3 = st.columns(3)
      strategy_filter = fc1.selectbox("전략", ["전체", "day", "swing"])
      result_filter = fc2.selectbox("결과", ["전체", "PENDING", "WIN", "LOSS", "EXPIRED"])
      actual_buy_filter = fc3.selectbox("실매매", ["전체", "Y", "N"])

      df_display = df_hist.copy()

      if strategy_filter != "전체":
        df_display = df_display[df_display["strategy"] == strategy_filter]

      if result_filter != "전체":
        if result_filter == "PENDING":
          df_display = df_display[df_display["result"].apply(
            lambda x: str(x).strip() in ("", "PENDING", "None")
          )]
        else:
          df_display = df_display[df_display["result"] == result_filter]

      if actual_buy_filter != "전체":
        if "actual_buy" in df_display.columns:
          df_display = df_display[
            df_display["actual_buy"].astype(str).str.upper() == actual_buy_filter
          ]

      # 결과 색상 스타일 함수
      def color_result(val: object) -> str:
        """결과 컬럼 색상 매핑."""
        return {
          "WIN":     "color: #00cc44; font-weight: bold",
          "LOSS":    "color: #ff4444; font-weight: bold",
          "EXPIRED": "color: #888888",
          "PENDING": "color: #ffaa00",
        }.get(str(val), "")

      if "result" in df_display.columns:
        styled = df_display.style.map(color_result, subset=["result"])
        st.dataframe(styled, use_container_width=True)
      else:
        st.dataframe(df_display, use_container_width=True)
