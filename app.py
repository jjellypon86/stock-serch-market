from datetime import datetime

import pandas as pd
import streamlit as st

from backtest import run_backtest
from scanner import scan_day_trading, scan_swing
from sheets import evaluate_strategy, is_configured, load_history, save_scan_results, update_results
from utils import get_last_trading_date, get_market_direction, get_stock_news

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

if "df_day" not in st.session_state:
    st.session_state.df_day = pd.DataFrame()
if "df_swing" not in st.session_state:
    st.session_state.df_swing = pd.DataFrame()

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
        # 가격 2×2 배치 (숫자가 길어도 잘리지 않도록)
        r1c1, r1c2 = st.columns(2)
        r1c1.metric(
            "매수 참고가 💰",
            f"{row['buy_price']:,}원",
            delta="다음날 시초가 ±1%",
            delta_color="off",
        )
        r1c2.metric("현재가", f"{int(row['close']):,}원")

        r2c1, r2c2 = st.columns(2)
        r2c1.metric(
            "익절가 🎯",
            f"{row['take_profit']:,}원",
            delta=f"+{round((row['take_profit'] / row['close'] - 1) * 100, 1)}%",
        )
        r2c2.metric(
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
    """베스트 3 요약 카드"""
    top3 = df.head(3)
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
    with st.expander("📖 조건 해설 & 매수 가이드"):
        st.markdown("""
**조건 해설**

| 조건 | 의미 |
|------|------|
| 종가 > MA20 > MA60 | 단기·중기 모두 우상향 중인 종목. 추세가 살아있는 상태에서만 진입 |
| 최근 3일 중 하락 2일↑ | 상승 추세 중 단기 쉬어가는 구간(눌림) 확인. 조정이 너무 짧으면 아직 눌림 미완성 |
| MA20 ±3% 이내 | 20일선이 지지선 역할을 할 수 있는 가격대. 너무 멀면 반등 폭이 불확실 |
| 거래량 감소 | 눌림 중 매도세가 약해진 신호. 거래량 없이 빠지는 건 공포 매도가 아닌 자연스러운 조정 |
| 기관/외국인 순매수 2일↑ | 스마트머니가 이 가격대를 좋게 본다는 뜻. 개인 혼자 받치는 종목은 제외 |

---

**매수 가이드**

- **진입 타이밍**: 스캔 다음날 장 시작 후 **시초가 ±1% 이내**에서 매수. 갭 상승으로 이미 많이 올랐으면 진입 포기
- **목표 수익**: 익절가까지 +5% 내외 (ATR 기반으로 종목마다 다름)
- **손절 원칙**: 손절가 터치 즉시 매도. 기다리면 손실 커짐
- **만기 원칙**: 5거래일 내 익절/손절 없으면 **무조건 청산**. 미련 금지
- **분할 매수 금지**: 손절가 아래로 더 빠졌을 때 추가 매수는 전략 외 행동
""")


    if st.button("🔍 단기 스캔 시작", key="btn_day"):
        mkt = get_market_direction(date_str)
        if mkt["trend"] == "하락":
            st.warning(
                f"⚠️ KOSPI {mkt['kospi']:,} / MA20 {mkt['ma20']:,} ({mkt['gap_pct']:+.1f}%)"
                " — 지수 하락 구간. 개별 신호 신뢰도 낮을 수 있음"
            )
        with st.spinner("스캔 중..."):
            st.session_state.df_day = scan_day_trading(date_str, market)
        if st.session_state.df_day.empty:
            st.info("조건에 맞는 종목이 없습니다.")
        else:
            saved, err, n_saved, n_skip = save_scan_results(st.session_state.df_day, "day", market, date_str)
            parts = ([f"{n_saved}개 저장"] if n_saved else []) + ([f"{n_skip}개 중복 건너뜀"] if n_skip else [])
            detail = " / ".join(parts) if parts else "변경 없음"
            if saved:
                st.success(f"베스트 {len(st.session_state.df_day)}개 최종 추천 — {detail}")
            else:
                st.success(f"베스트 {len(st.session_state.df_day)}개 최종 추천")
                if err:
                    st.error(f"Sheets 저장 실패: {err}")

    df_day = st.session_state.df_day
    if not df_day.empty:
        render_metric_cards(df_day)

        st.divider()
        st.subheader("베스트 3 최종 추천 상세")
        st.caption("매수 참고가 기준: 스캔 당일 종가 / 다음날 시초가 ±1% 이내 진입 권장")

        for _, row in df_day.iterrows():
            render_stock_card(row)

        st.divider()
        st.subheader("선정 결과")
        df_day_display = df_day.rename(columns={
            "ticker": "종목코드", "name": "종목명",
            "buy_price": "매수참고가", "close": "현재가",
            "pullback_pct": "눌림(%)", "vol_ratio": "거래량비율",
            "inst_days": "기관순매수일", "foreign_days": "외국인순매수일",
            "take_profit": "익절가", "stop_loss": "손절가",
            "risk_reward": "손익비", "net_profit_pct": "예상수익률(%)",
        })
        st.dataframe(
            df_day_display.style.format({
                "매수참고가": "{:,}", "현재가": "{:,}",
                "익절가": "{:,}", "손절가": "{:,}",
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
    with st.expander("📖 조건 해설 & 매수 가이드"):
        st.markdown("""
**조건 해설**

| 조건 | 의미 |
|------|------|
| 종가 > MA60 > MA120 | 중기·장기 모두 우상향. 단기 전략보다 더 긴 추세가 살아있는 종목만 |
| 최근 5일 중 하락 3일↑ | 1주일 가량의 조정 구간 확인. 충분히 눌렸을 때 진입해야 반등 여력이 생김 |
| MA60 ±3% 이내 | 60일선 근처에서 지지를 받을 수 있는 가격대 |
| 거래량 감소 | 조정 중 매도세 소진. 거래량이 줄며 빠지는 건 건강한 눌림 |
| 기관/외국인 순매수 2일↑ | 중장기 투자자(기관/외국인)가 이 가격대를 매집 구간으로 보는 신호 |

---

**매수 가이드**

- **진입 타이밍**: 스캔 다음날 장 시작 후 **시초가 ±1% 이내**에서 매수. 단기보다 갭 변동이 크므로 주의
- **목표 수익**: 익절가까지 +7% 내외 (ATR 기반으로 종목마다 다름)
- **손절 원칙**: 손절가(-3%) 터치 즉시 매도. MA60을 완전히 이탈하면 추세 전환 가능성 높음
- **만기 원칙**: 10거래일(2주) 내 결판. 2주가 지나도 안 움직이는 종목은 추진력 없는 것
- **뉴스 확인**: 스윙은 보유 기간이 길어서 중간에 실적·공시 이슈가 터질 수 있음. 매수 후에도 뉴스 모니터링 권장
""")


    if st.button("🔍 스윙 스캔 시작", key="btn_swing"):
        mkt = get_market_direction(date_str)
        if mkt["trend"] == "하락":
            st.warning(
                f"⚠️ KOSPI {mkt['kospi']:,} / MA20 {mkt['ma20']:,} ({mkt['gap_pct']:+.1f}%)"
                " — 지수 하락 구간. 개별 신호 신뢰도 낮을 수 있음"
            )
        with st.spinner("스캔 중... (전 종목 분석으로 수 분 소요될 수 있습니다)"):
            st.session_state.df_swing = scan_swing(date_str, market)
        if st.session_state.df_swing.empty:
            st.info("조건에 맞는 종목이 없습니다.")
        else:
            saved, err, n_saved, n_skip = save_scan_results(st.session_state.df_swing, "swing", market, date_str)
            parts = ([f"{n_saved}개 저장"] if n_saved else []) + ([f"{n_skip}개 중복 건너뜀"] if n_skip else [])
            detail = " / ".join(parts) if parts else "변경 없음"
            if saved:
                st.success(f"베스트 {len(st.session_state.df_swing)}개 최종 추천 — {detail}")
            else:
                st.success(f"베스트 {len(st.session_state.df_swing)}개 최종 추천")
                if err:
                    st.error(f"Sheets 저장 실패: {err}")

    df_swing = st.session_state.df_swing
    if not df_swing.empty:
        render_metric_cards(df_swing)

        st.divider()
        st.subheader("베스트 3 최종 추천 상세")
        st.caption("매수 참고가 기준: 스캔 당일 종가 / 다음날 시초가 ±1% 이내 진입 권장")

        for _, row in df_swing.iterrows():
            render_stock_card(row)

        st.divider()
        st.subheader("선정 결과")
        df_swing_display = df_swing.rename(columns={
            "ticker": "종목코드", "name": "종목명",
            "buy_price": "매수참고가", "close": "현재가",
            "pullback_pct": "눌림(%)", "vol_ratio": "거래량비율",
            "inst_days": "기관순매수일", "foreign_days": "외국인순매수일",
            "ma60": "MA60", "ma120": "MA120",
            "take_profit": "익절가", "stop_loss": "손절가",
            "risk_reward": "손익비",
        })
        st.dataframe(
            df_swing_display.style.format({
                "매수참고가": "{:,}", "현재가": "{:,}",
                "MA60": "{:,}", "MA120": "{:,}",
                "익절가": "{:,}", "손절가": "{:,}",
            }),
            use_container_width=True,
            hide_index=True,
        )

# 백테스트 탭
with tab_backtest:
    st.subheader("전략 백테스트")
    st.caption(
        f"시총 1000억↑ 종목 고정 샘플 {200}개 대상 (random_state=42, 재현 가능) / "
        "신호 발생 다음날 시가(+슬리피지 0.1%) 매수 / 익절·손절·기간만료 시 청산 / 복리 MDD 기준"
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
                n, err = update_results()
            if err:
                st.error(f"업데이트 오류: {err}")
            elif n > 0:
                load_history.clear()
                st.success(f"{n}개 종목 결과 업데이트 완료")
            else:
                st.info("업데이트할 항목이 없습니다.")

        df_hist = load_history()

        if df_hist.empty:
            st.info("저장된 히스토리가 없습니다. 스캔을 먼저 실행해 주세요.")
        else:
            df_done = df_hist[df_hist["result"].isin(["WIN", "LOSS", "EXPIRED"])].copy()

            n_total   = len(df_hist)
            n_done    = len(df_done)
            n_pending = df_hist["result"].apply(lambda x: str(x).strip() in ("", "PENDING", "None")).sum()
            n_error   = (df_hist["result"] == "ERROR").sum()

            sc1, sc2, sc3, sc4 = st.columns(4)
            sc1.metric("전체 기록", n_total)
            sc2.metric("완료 (WIN/LOSS/EXPIRED)", n_done)
            sc3.metric("대기 중 (PENDING)", n_pending)
            sc4.metric("오류 (ERROR)", n_error)

            if n_done == 0:
                st.info("아직 완료된 거래가 없습니다. 단기 전략은 스캔일 기준 5거래일, 스윙은 10거래일 이후 결과 업데이트를 실행해 주세요.")
            else:
                st.divider()

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
            if "ticker" in df_hist.columns:
                df_hist = df_hist.copy()

                def _fmt_ticker(x: object) -> str:
                    if not pd.notna(x):
                        return ""
                    s = str(x).strip()
                    if s in ("", "None", "nan"):
                        return ""
                    try:
                        return str(int(float(s))).zfill(6)
                    except (ValueError, OverflowError):
                        return s

                df_hist["ticker"] = df_hist["ticker"].apply(_fmt_ticker)

            # 필터 컨트롤
            f1, f2, f3, f4 = st.columns(4)
            sel_strategy = f1.selectbox("전략", ["전체", "단기", "스윙"], key="hist_strategy")
            sel_market   = f2.selectbox("시장", ["전체", "KOSPI", "KOSDAQ"], key="hist_market")
            sel_result   = f3.selectbox("결과", ["전체", "WIN", "LOSS", "EXPIRED", "PENDING"], key="hist_result")
            sel_period   = f4.selectbox("기간", ["전체", "최근 30일", "최근 60일", "최근 90일"], key="hist_period")

            # 필터 적용
            df_disp = df_hist.copy()
            strategy_map = {"단기": "day", "스윙": "swing"}
            if sel_strategy != "전체":
                df_disp = df_disp[df_disp["strategy"] == strategy_map[sel_strategy]]
            if sel_market != "전체":
                df_disp = df_disp[df_disp["market"] == sel_market]
            if sel_result != "전체":
                if sel_result == "PENDING":
                    df_disp = df_disp[df_disp["result"].apply(
                        lambda x: str(x).strip() in ("", "PENDING", "None")
                    )]
                else:
                    df_disp = df_disp[df_disp["result"] == sel_result]
            if sel_period != "전체":
                days_map = {"최근 30일": 30, "최근 60일": 60, "최근 90일": 90}
                cutoff = (pd.Timestamp.today() - pd.Timedelta(days=days_map[sel_period])).strftime("%Y%m%d")
                df_disp = df_disp[df_disp["scan_date"].astype(str) >= cutoff]

            # 최신순 정렬
            df_disp = df_disp.sort_values("scan_date", ascending=False)

            # 핵심 컬럼 / 상세 컬럼 분리
            show_detail = st.checkbox("상세 컬럼 보기", key="hist_detail")
            base_cols  = ["scan_date", "strategy", "market", "ticker", "name",
                          "buy_price", "take_profit", "stop_loss", "result", "profit_pct", "actual_buy"]
            extra_cols = ["entry_price", "risk_reward", "pullback_pct", "inst_days", "foreign_days", "hold_days"]
            display_cols = base_cols + (extra_cols if show_detail else [])
            df_disp = df_disp[[c for c in display_cols if c in df_disp.columns]]

            st.caption(f"총 {len(df_hist)}건 중 {len(df_disp)}건 표시")

            col_rename = {
                "scan_date": "스캔일", "strategy": "전략", "market": "시장",
                "ticker": "종목코드", "name": "종목명",
                "buy_price": "매수참고가", "entry_price": "진입가",
                "take_profit": "익절가", "stop_loss": "손절가",
                "risk_reward": "손익비", "pullback_pct": "눌림(%)",
                "inst_days": "기관", "foreign_days": "외국인",
                "result": "결과", "profit_pct": "수익률(%)", "hold_days": "보유일",
                "actual_buy": "실거래",
            }
            df_renamed = df_disp.rename(columns=col_rename)

            _result_colors: dict[str, str] = {
                "WIN": "background-color: #d4edda",
                "LOSS": "background-color: #f8d7da",
                "EXPIRED": "background-color: #e2e3e5",
                "PENDING": "background-color: #fff3cd",
            }

            def _color_result(val: object) -> str:
                return _result_colors.get(str(val), "")

            price_cols = [c for c in ["매수참고가", "진입가", "익절가", "손절가"] if c in df_renamed.columns]
            pct_cols   = [c for c in ["수익률(%)", "눌림(%)"] if c in df_renamed.columns]
            num_cols   = price_cols + pct_cols + (["손익비"] if "손익비" in df_renamed.columns else [])
            for c in num_cols:
                df_renamed[c] = pd.to_numeric(df_renamed[c], errors="coerce")

            def _price_fmt(x: object) -> str:
                try:
                    return f"{float(x):,.0f}"  # type: ignore[arg-type]
                except (ValueError, TypeError):
                    return "-"

            def _pct_fmt(x: object) -> str:
                try:
                    return f"{float(x):.2f}"  # type: ignore[arg-type]
                except (ValueError, TypeError):
                    return "-"

            def _rr_fmt(x: object) -> str:
                try:
                    return f"{float(x):.1f}"  # type: ignore[arg-type]
                except (ValueError, TypeError):
                    return "-"

            fmt: dict[str, object] = {c: _price_fmt for c in price_cols}
            fmt.update({c: _pct_fmt for c in pct_cols})
            if "손익비" in df_renamed.columns:
                fmt["손익비"] = _rr_fmt

            result_col = "결과"
            if result_col in df_renamed.columns:
                styled = df_renamed.style.map(_color_result, subset=[result_col]).format(fmt)
                st.dataframe(styled, use_container_width=True, hide_index=True)
            else:
                st.dataframe(df_renamed.style.format(fmt), use_container_width=True, hide_index=True)

st.divider()
st.markdown("""
## 📓 K-QUANT TRACKER 사용 규칙

> *"이 스캐너를 손에 넣은 자는 시장을 신의 시점으로 바라볼 수 있다.
> 단, 규칙을 어기는 자는 시장이 반드시 심판한다."*

---

**제 1 규칙** — 추천 종목은 **다음날 시초가 ±1% 이내**에서만 진입하라. 그 선을 넘어 쫓아가는 것은 이미 진 싸움이다.

**제 2 규칙** — **KOSPI 경고가 뜬 날**은 포지션을 반으로 줄여라. 지수가 흔들리는 날, 개별 신호는 노이즈다.

**제 3 규칙** — **손절가에 닿는 순간 즉시 매도**하라. "조금만 더..." 라고 생각하는 순간 그것은 전략이 아니라 감정이다.

**제 4 규칙** — 단기 **5일**, 스윙 **10일**. 기간이 끝나면 미련 없이 청산하라. 시간은 곧 기회비용이다.

**제 5 규칙** — 검증 탭 **자가 진단 60점 미만**이 되면 신규 진입을 즉시 멈춰라. 데이터는 거짓말하지 않는다.

---

**[ 시나리오: 완벽한 하루 ]**
> KOSPI 경고 없음. 베스트 3 중 손익비 2.3, 기관 3일 연속 순매수. 다음날 시초가 진입 → 익절 → Sheets에 Y 기입. 30건이 쌓이면 검증 탭을 열어라. 점수가 82점이라면, 전략은 살아있다.

**[ 시나리오: 손절하는 날 ]**
> 손절가에 닿는다. 손이 멈칫한다. — 그 순간 제 3 규칙을 떠올려라. 즉시 매도. 손실 -2%는 실패가 아니다. 전략이 설계대로 작동한 것이다.

**[ 시나리오: 전략을 바꿔야 할 때 ]**
> 자가 진단 54점. 신규 진입을 멈추고 백테스트로 파라미터를 바꿔라. 점수가 80점을 회복하기 전까지 실전은 없다. 규율 없는 자에게 시장은 자비를 베풀지 않는다.
""")
