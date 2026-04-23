from datetime import datetime

import pandas as pd
import streamlit as st

from backtest import run_backtest
from scanner import scan_day_trading, scan_swing
from sheets import evaluate_strategy, is_configured, load_history, save_scan_results, update_results
from utils import get_last_trading_date, get_stock_news

st.set_page_config(
    page_title="K-Quant Tracker",
    page_icon="📈",
    layout="centered",
)

st.title("📈 K-Quant Tracker")
st.caption("한국 주식 눌림목 + 수급 기반 종목 스캐너")

with st.sidebar:
    st.header("스캔 설정")
    market = st.selectbox("시장 선택", ["KOSPI", "KOSDAQ"], index=0)
    default_date = get_last_trading_date()
    scan_date = st.date_input("조회 날짜", value=datetime.strptime(default_date, "%Y%m%d").date())
    date_str = scan_date.strftime("%Y%m%d") if scan_date else default_date
    st.caption(f"조회 기준일: {date_str[:4]}.{date_str[4:6]}.{date_str[6:]}")

tab_day, tab_swing, tab_backtest, tab_verify = st.tabs([
    "📊 단기 (당일 매매)", "📅 스윙 (1주일)", "🔬 백테스트", "📈 검증"
])


def render_stock_card(row: pd.Series) -> None:
    """종목 카드: 매수가 / 현재가 / 익절가 / 손절가 / 수급 / 뉴스"""
    expander_label = (
        f"**{row['name']}** ({row['ticker']})"
        f"  —  눌림 {row['pullback_pct']:+.1f}%"
        f"  /  손익비 {row['risk_reward']}:1"
    )
    with st.expander(expander_label):
        # 가격 4칸
        c1, c2, c3, c4 = st.columns(4)
        c1.metric(
            "매수 참고가 💰",
            f"{row['buy_price']:,}원",
            delta="다음날 시초가 ±1%",
            delta_color="off",
        )
        c2.metric("현재가", f"{int(row['close']):,}원")
        c3.metric(
            "익절가 🎯",
            f"{row['take_profit']:,}원",
            delta=f"+{round((row['take_profit'] / row['close'] - 1) * 100, 1)}%",
        )
        c4.metric(
            "손절가 🛑",
            f"{row['stop_loss']:,}원",
            delta=f"{round((row['stop_loss'] / row['close'] - 1) * 100, 1)}%",
            delta_color="inverse",
        )

        st.divider()

        # 수급 정보
        inst_days = row.get("inst_days", 0)
        foreign_days = row.get("foreign_days", 0)
        if inst_days == 0 and foreign_days == 0:
            st.caption("수급 데이터 미확인")
        else:
            col_i, col_f = st.columns(2)
            col_i.info(f"🏦 기관 3일중 **{inst_days}일** 순매수")
            col_f.info(f"🌍 외국인 3일중 **{foreign_days}일** 순매수")

        st.caption(
            f"거래량 비율: {row.get('vol_ratio', 0):.2f}배  |  "
            f"눌림폭: {row['pullback_pct']:+.2f}%"
        )

        st.divider()

        # 관련 뉴스
        news_list = get_stock_news(row["ticker"])
        if news_list:
            st.markdown("**📰 관련 뉴스**")
            for news in news_list:
                st.markdown(f"- [{news['title']}]({news['url']}) `{news['date']}`")
        else:
            st.caption("뉴스 데이터 없음")


def render_metric_cards(df: pd.DataFrame) -> None:
    """베스트 2 요약 카드"""
    top3 = df.head(2)
    cols = st.columns(len(top3))
    for col, (_, row) in zip(cols, top3.iterrows()):
        with col:
            st.metric(
                label=row["name"],
                value=f"{int(row['close']):,}원",
                delta=f"눌림 {row['pullback_pct']:+.1f}%",
            )


# 단기 탭
with tab_day:
    st.subheader("단기 눌림목 종목")
    st.caption(
        "조건: 종가 > MA20 > MA60 & 최근 3일 중 하락 2일↑ & MA20 ±3% 이내 "
        "& 거래량 감소 & 기관/외국인 3일중 2일↑ 순매수"
    )

    if st.button("🔍 단기 스캔 시작", key="btn_day"):
        with st.spinner("스캔 중..."):
            df_day = scan_day_trading(date_str, market)

        if df_day.empty:
            st.info("조건에 맞는 종목이 없습니다.")
        else:
            saved = save_scan_results(df_day, "day", market, date_str)
            if saved:
                st.success(f"베스트 {len(df_day)}개 최종 추천 — Google Sheets 저장 완료")
            else:
                st.success(f"베스트 {len(df_day)}개 최종 추천")
            render_metric_cards(df_day)

            st.divider()
            st.subheader("베스트 2 최종 추천 상세")
            st.caption("매수 참고가 기준: 스캔 당일 종가 / 다음날 시초가 ±1% 이내 진입 권장")

            for _, row in df_day.iterrows():
                render_stock_card(row)

            st.divider()
            st.subheader("선정 결과")
            st.dataframe(
                df_day.rename(columns={
                    "ticker": "종목코드",
                    "name": "종목명",
                    "buy_price": "매수참고가",
                    "close": "현재가",
                    "pullback_pct": "눌림(%)",
                    "vol_ratio": "거래량비율",
                    "inst_days": "기관순매수일",
                    "foreign_days": "외국인순매수일",
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
    st.subheader("스윙 눌림목 종목")
    st.caption(
        "조건: 종가 > MA60 > MA120 & 최근 5일 중 하락 3일↑ & MA60 ±3% 이내 "
        "& 거래량 감소 & 기관/외국인 3일중 2일↑ 순매수"
    )

    if st.button("🔍 스윙 스캔 시작", key="btn_swing"):
        with st.spinner("스캔 중... (전 종목 분석으로 수 분 소요될 수 있습니다)"):
            df_swing = scan_swing(date_str, market)

        if df_swing.empty:
            st.info("조건에 맞는 종목이 없습니다.")
        else:
            saved = save_scan_results(df_swing, "swing", market, date_str)
            if saved:
                st.success(f"베스트 {len(df_swing)}개 최종 추천 — Google Sheets 저장 완료")
            else:
                st.success(f"베스트 {len(df_swing)}개 최종 추천")
            render_metric_cards(df_swing)

            st.divider()
            st.subheader("베스트 2 최종 추천 상세")
            st.caption("매수 참고가 기준: 스캔 당일 종가 / 다음날 시초가 ±1% 이내 진입 권장")

            for _, row in df_swing.iterrows():
                render_stock_card(row)

            st.divider()
            st.subheader("선정 결과")
            st.dataframe(
                df_swing.rename(columns={
                    "ticker": "종목코드",
                    "name": "종목명",
                    "buy_price": "매수참고가",
                    "close": "현재가",
                    "pullback_pct": "눌림(%)",
                    "vol_ratio": "거래량비율",
                    "inst_days": "기관순매수일",
                    "foreign_days": "외국인순매수일",
                    "ma60": "MA60",
                    "ma120": "MA120",
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
    st.caption(
        f"시총 300억↑ 종목 랜덤 {200}개 대상 / 신호 발생 다음날 시가(+슬리피지 0.1%) 매수 / "
        "익절·손절·기간만료 시 청산 / 복리 MDD 기준"
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        bt_strategy = st.selectbox(
            "전략", ["day", "swing"],
            format_func=lambda x: "단기 눌림목(MA20)" if x == "day" else "스윙 눌림목(MA60)"
        )
    with col2:
        bt_months = st.selectbox("기간", [1, 3, 6], index=1, format_func=lambda x: f"{x}개월")
    with col3:
        bt_market = st.selectbox("시장", ["KOSPI", "KOSDAQ"], key="bt_market")

    if bt_strategy == "day":
        st.info("익절 +5% / 손절 -2% / 최대 보유 5거래일")
    else:
        st.info("익절 +7% / 손절 -3% / 최대 보유 10거래일")

    if st.button("🔬 백테스트 실행", key="btn_backtest"):
        with st.spinner(f"백테스트 중... ({bt_months}개월 / 랜덤 200종목)"):
            result = run_backtest(bt_strategy, bt_months, bt_market)

        if not result:
            st.warning("백테스트 결과가 없습니다. 데이터를 확인해 주세요.")
        else:
            win_rate = result["승률(%)"]
            expectancy = result["기대값(%)"]

            if win_rate >= 70:
                st.success(f"승률 {win_rate}% — 목표 달성 ✅")
            elif win_rate >= 60:
                st.warning(f"승률 {win_rate}% — 조건 강화 필요")
            else:
                st.error(f"승률 {win_rate}% — 전략 재검토 필요")

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("총 거래수", result["총 거래수"])
            m2.metric("승률", f"{win_rate}%")
            m3.metric(
                "기대값",
                f"{expectancy}%",
                delta="양수=수익" if expectancy > 0 else "음수=손실",
                delta_color="normal" if expectancy > 0 else "inverse",
            )
            m4.metric("MDD", f"{result['MDD(%)']}%", delta_color="inverse")

            st.divider()
            col_a, col_b = st.columns(2)
            with col_a:
                st.metric("평균 수익(익절)", f"{result['평균 수익(%)']}%")
                st.metric("익절 횟수", result["익절"])
            with col_b:
                st.metric("평균 손실(손절+만료)", f"{result['평균 손실(%)']}%")
                st.metric("손절+만료 횟수", result["손절+만료"])

            if result.get("trades"):
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

# 검증 탭
with tab_verify:
    st.subheader("전략 검증")

    if not is_configured():
        st.warning("Google Sheets가 연동되지 않았습니다. Streamlit Cloud Secrets에 `gcp_service_account`를 설정해 주세요.")
    else:
        if st.button("🔄 결과 업데이트", key="btn_update"):
            with st.spinner("미완료 종목 결과 자동 판정 중..."):
                n = update_results()
            st.success(f"{n}개 종목 결과 업데이트 완료") if n > 0 else st.info("업데이트할 항목이 없습니다.")

        df_hist = load_history()

        if df_hist.empty:
            st.info("저장된 히스토리가 없습니다. 스캔을 먼저 실행해 주세요.")
        else:
            df_done = df_hist[df_hist["result"].isin(["WIN", "LOSS", "EXPIRED"])].copy()

            if not df_done.empty:
                df_done["profit_pct"] = pd.to_numeric(df_done["profit_pct"], errors="coerce")

                st.subheader("전략 검증 통계 (전체 추천 기준)")
                wins      = (df_done["result"] == "WIN").sum()
                total     = len(df_done)
                win_rate  = round(wins / total * 100, 1)
                expectancy = round(df_done["profit_pct"].mean(), 2)

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("총 신호수", total)
                c2.metric("승률", f"{win_rate}%")
                c3.metric("기대값", f"{expectancy}%",
                          delta="양수=수익" if expectancy > 0 else "음수=손실",
                          delta_color="normal" if expectancy > 0 else "inverse")
                c4.metric("평균 수익(WIN)", f"{round(df_done[df_done['result']=='WIN']['profit_pct'].mean(), 2)}%")

                df_actual = df_done[df_done["actual_buy"].astype(str).str.upper() == "Y"]
                if not df_actual.empty:
                    st.divider()
                    st.subheader("실거래 추적 (actual_buy=Y)")
                    wins_a     = (df_actual["result"] == "WIN").sum()
                    total_a    = len(df_actual)
                    wr_a       = round(wins_a / total_a * 100, 1)
                    expect_a   = round(df_actual["profit_pct"].mean(), 2)
                    ca1, ca2, ca3 = st.columns(3)
                    ca1.metric("실거래 횟수", total_a)
                    ca2.metric("실거래 승률", f"{wr_a}%")
                    ca3.metric("실거래 기대값", f"{expect_a}%",
                               delta_color="normal" if expect_a > 0 else "inverse")

            st.divider()
            st.subheader("전략 자가 진단")

            for strategy_name, strategy_label in [("day", "단기"), ("swing", "스윙")]:
                df_strat = df_done[df_done["strategy"] == strategy_name].copy()
                n = len(df_strat)
                st.markdown(f"**{strategy_label} 전략** — {n}건 완료")
                if n < 30:
                    st.info(f"판단 시작까지 {30 - n}건 더 필요 (최소 30건)")
                    continue

                diag = evaluate_strategy(df_strat)
                score   = diag["score"]
                verdict = diag["verdict"]

                col_score, col_bars = st.columns([1, 2])
                with col_score:
                    delta_txt = "✅ 합격" if verdict == "합격" else ("⚠️ 경고" if verdict == "경고" else "❌ 재검토")
                    delta_clr = "normal" if verdict == "합격" else ("off" if verdict == "경고" else "inverse")
                    st.metric("종합 점수", f"{score}점", delta=delta_txt, delta_color=delta_clr)

                with col_bars:
                    bd = diag["breakdown"]
                    for label, key in [("승률", "win_rate"), ("기대값", "expected_value"), ("MDD", "mdd"), ("손익비", "pl_ratio")]:
                        item = bd[key]
                        st.caption(f"{label}  {item['score']}/{item['max']}점  (현재 {item['value']})")
                        st.progress(item["score"] / item["max"] if item["max"] > 0 else 0)

                if diag["weak_points"]:
                    msg = "  \n".join(f"• {p}" for p in diag["weak_points"])
                    st.error(msg) if verdict == "재검토" else st.warning(msg)

            st.divider()
            st.subheader("추천 히스토리")
            st.dataframe(
                df_hist.rename(columns={
                    "scan_date": "스캔일", "strategy": "전략", "market": "시장",
                    "ticker": "종목코드", "name": "종목명",
                    "buy_price": "매수참고가", "entry_price": "진입가",
                    "take_profit": "익절가", "stop_loss": "손절가",
                    "risk_reward": "손익비", "pullback_pct": "눌림(%)",
                    "inst_days": "기관", "foreign_days": "외국인",
                    "result": "결과", "profit_pct": "수익률(%)", "hold_days": "보유일",
                    "actual_buy": "실거래",
                }),
                use_container_width=True,
                hide_index=True,
            )
