import streamlit as st

from backtest import run_backtest
from scanner import scan_day_trading, scan_swing
from utils import get_last_trading_date

st.set_page_config(
    page_title="K-Quant Tracker",
    page_icon="📈",
    layout="centered",
)

st.title("📈 K-Quant Tracker")
st.caption("한국 주식 단기/스윙 종목 스캐너")

# 사이드바 설정
with st.sidebar:
    st.header("스캔 설정")
    market = st.selectbox("시장 선택", ["KOSPI", "KOSDAQ"], index=0)
    default_date = get_last_trading_date()
    scan_date = st.date_input("조회 날짜", value=None, help="기본값: 직전 거래일")
    date_str = scan_date.strftime("%Y%m%d") if scan_date else default_date
    st.caption(f"조회 기준일: {date_str[:4]}.{date_str[4:6]}.{date_str[6:]}")

tab_day, tab_swing, tab_backtest = st.tabs(["📊 단기 (당일 매매)", "📅 스윙 (1주일)", "🔬 백테스트"])


def render_metric_cards(df: "pd.DataFrame", delta_col: str, label: str) -> None:
    """상위 3종목 metric 카드 렌더링"""
    top3 = df.head(3)
    cols = st.columns(len(top3))
    for col, (_, row) in zip(cols, top3.iterrows()):
        with col:
            st.metric(
                label=row["name"],
                value=f"{int(row['close']):,}원",
                delta=f"{row[delta_col]}{label}",
            )


def render_exit_guide(row: "pd.Series") -> None:
    """개별 종목 익절/손절 가이드 인라인 표시"""
    c1, c2, c3 = st.columns(3)
    c1.metric("현재가", f"{int(row['close']):,}원")
    c2.metric("익절가 🎯", f"{int(row['take_profit']):,}원",
              delta=f"+{round((row['take_profit']/row['close']-1)*100, 1)}%")
    c3.metric("손절가 🛑", f"{int(row['stop_loss']):,}원",
              delta=f"{round((row['stop_loss']/row['close']-1)*100, 1)}%",
              delta_color="inverse")


# 단기 탭
with tab_day:
    st.subheader("거래량 급등 + RSI 반등 종목")
    st.caption("조건: 거래량 200%↑ & RSI 30 상향돌파 & 양봉 마감 & 거래대금 10억↑ & 시총 300억↑")

    if st.button("🔍 단기 스캔 시작", key="btn_day"):
        with st.spinner("스캔 중..."):
            df_day = scan_day_trading(date_str, market)

        if df_day.empty:
            st.info("조건에 맞는 종목이 없습니다.")
        else:
            st.success(f"{len(df_day)}개 종목 발견")
            render_metric_cards(df_day, "volume_ratio", "배")

            st.divider()
            st.subheader("익절/손절 가이드 (ATR 기반)")
            st.caption("익절: ATR×2 / 손절: ATR×1 / 손익비(R:R) 2 이상 권장")

            for _, row in df_day.iterrows():
                with st.expander(f"**{row['name']}** ({row['ticker']})  —  손익비 {row['risk_reward']}:1"):
                    render_exit_guide(row)

            st.divider()
            st.subheader("전체 결과")
            st.dataframe(
                df_day.rename(columns={
                    "ticker": "종목코드",
                    "name": "종목명",
                    "close": "현재가",
                    "volume_ratio": "거래량비율(배)",
                    "rsi_prev": "전일RSI",
                    "rsi_today": "당일RSI",
                    "take_profit": "익절가",
                    "stop_loss": "손절가",
                    "risk_reward": "손익비",
                    "net_profit_pct": "예상수익률(%)",
                }),
                use_container_width=True,
                hide_index=True,
            )

# 스윙 탭
with tab_swing:
    st.subheader("골든크로스 종목")
    st.caption("조건: MA5/MA20 골든크로스 & 거래대금 10억↑ & 시총 300억↑ (이격도 낮은 순 = 골든크로스 초기)")

    if st.button("🔍 스윙 스캔 시작", key="btn_swing"):
        with st.spinner("스캔 중... (전 종목 분석으로 수 분 소요될 수 있습니다)"):
            df_swing = scan_swing(date_str, market)

        if df_swing.empty:
            st.info("조건에 맞는 종목이 없습니다.")
        else:
            st.success(f"{len(df_swing)}개 종목 발견")
            render_metric_cards(df_swing, "ma_gap_pct", "%")

            st.divider()
            st.subheader("익절/손절 가이드 (ATR 기반)")
            st.caption("익절: ATR×3 / 손절: ATR×1.5 / 최대 보유 10거래일 권장")

            for _, row in df_swing.iterrows():
                with st.expander(f"**{row['name']}** ({row['ticker']})  —  MA 이격도 {row['ma_gap_pct']}%"):
                    render_exit_guide(row)

            st.divider()
            st.subheader("전체 결과")
            st.dataframe(
                df_swing.rename(columns={
                    "ticker": "종목코드",
                    "name": "종목명",
                    "close": "현재가",
                    "ma5": "MA5",
                    "ma20": "MA20",
                    "ma_gap_pct": "MA이격도(%)",
                    "take_profit": "익절가",
                    "stop_loss": "손절가",
                    "risk_reward": "손익비",
                }),
                use_container_width=True,
                hide_index=True,
            )

# 백테스트 탭
with tab_backtest:
    st.subheader("전략 백테스트")
    st.caption(f"시총 상위 150개 종목 대상 / 신호 발생 다음날 시가 매수 / 익절·손절·기간만료 시 청산")

    col1, col2, col3 = st.columns(3)
    with col1:
        bt_strategy = st.selectbox("전략", ["day", "swing"],
                                   format_func=lambda x: "단기 (당일)" if x == "day" else "스윙 (1주일)")
    with col2:
        bt_months = st.selectbox("기간", [1, 3, 6], index=1, format_func=lambda x: f"{x}개월")
    with col3:
        bt_market = st.selectbox("시장", ["KOSPI", "KOSDAQ"], key="bt_market")

    if bt_strategy == "day":
        st.info("익절 +5% / 손절 -2% / 최대 보유 5거래일")
    else:
        st.info("익절 +7% / 손절 -3% / 최대 보유 10거래일")

    if st.button("🔬 백테스트 실행", key="btn_backtest"):
        with st.spinner(f"백테스트 중... ({bt_months}개월 / 시총 상위 150종목)"):
            result = run_backtest(bt_strategy, bt_months, bt_market)

        if not result:
            st.warning("백테스트 결과가 없습니다. 데이터를 확인해 주세요.")
        else:
            win_rate = result["승률(%)"]
            expectancy = result["기대값(%)"]

            # 승률 등급 표시
            if win_rate >= 70:
                st.success(f"승률 {win_rate}% — 목표 달성 ✅")
            elif win_rate >= 60:
                st.warning(f"승률 {win_rate}% — 조건 강화 필요")
            else:
                st.error(f"승률 {win_rate}% — 전략 재검토 필요")

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("총 거래수", result["총 거래수"])
            m2.metric("승률", f"{win_rate}%")
            m3.metric("기대값", f"{expectancy}%",
                      delta="양수=수익" if expectancy > 0 else "음수=손실",
                      delta_color="normal" if expectancy > 0 else "inverse")
            m4.metric("MDD", f"{result['MDD(%)']}%", delta_color="inverse")

            st.divider()
            col_a, col_b = st.columns(2)
            with col_a:
                st.metric("평균 수익(익절)", f"{result['평균 수익(%)']}%")
                st.metric("익절 횟수", result["익절"])
            with col_b:
                st.metric("평균 손실(손절+만료)", f"{result['평균 손실(%)']}%")
                st.metric("손절+만료 횟수", result["손절+만료"])

            # 트레이드 상세 내역
            if result.get("trades"):
                import pandas as pd
                with st.expander("트레이드 상세 내역 보기"):
                    df_trades = pd.DataFrame(result["trades"])
                    st.dataframe(
                        df_trades[["ticker", "result", "profit_pct", "hold_days"]].rename(columns={
                            "ticker": "종목코드",
                            "result": "결과",
                            "profit_pct": "수익률(%)",
                            "hold_days": "보유일",
                        }),
                        use_container_width=True,
                        hide_index=True,
                    )
