from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import FinanceDataReader as fdr
import pandas as pd
import requests
import streamlit as st

_NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://m.stock.naver.com",
}


def get_last_trading_date() -> str:
    """직전 거래일 반환 (장 마감 15:30 KST 기준, YYYYMMDD 포맷)

    GitHub Actions·Streamlit Cloud 서버는 UTC라 서버 로컬 시간을 쓰면 하루 밀린다.
    """
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    if now.hour < 15 or (now.hour == 15 and now.minute < 30):
        target = now - timedelta(days=1)
    else:
        target = now
    while target.weekday() >= 5:
        target -= timedelta(days=1)
    return target.strftime("%Y%m%d")


def _to_fdr_date(yyyymmdd: str) -> str:
    """YYYYMMDD → YYYY-MM-DD 변환"""
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def _parse_naver_number(text: str) -> int:
    cleaned = text.strip().replace(",", "").replace("+", "").replace("\xa0", "").replace(" ", "")
    try:
        return int(cleaned)
    except ValueError:
        return 0


@st.cache_data(ttl=3600)
def get_stock_listing(market: str = "KOSPI") -> pd.DataFrame:
    """
    네이버 증권(m.stock.naver.com) 시가총액 순 API에서 종목 리스팅
    (finance.naver.com 구 페이지는 2026년 리뉴얼로 폐지되어 신규 모바일 API로 교체)
    시총 300억 미만 종목 도달 시 스캔 중단, pageSize 상한은 100
    """
    stocks: list[dict] = []
    MIN_CAP = 30_000_000_000  # 300억

    for page in range(1, 50):
        url = (
            f"https://m.stock.naver.com/api/stocks/marketValue/{market}"
            f"?page={page}&pageSize=100"
        )
        try:
            resp = requests.get(url, headers=_NAVER_HEADERS, timeout=10)
            items = resp.json().get("stocks", [])
        except Exception:
            break

        if not items:
            break

        stop = False
        for item in items:
            try:
                market_cap = int(item["marketValueRaw"])
            except (KeyError, ValueError, TypeError):
                continue

            if market_cap < MIN_CAP:
                stop = True
                break

            stocks.append({
                "ticker": item["itemCode"],
                "name": item["stockName"],
                "종가": int(item["closePriceRaw"]),
                "거래대금": int(item["accumulatedTradingValueRaw"]),
                "market_cap": market_cap,
                "거래량": int(item["accumulatedTradingVolumeRaw"]),
            })

        if stop:
            break

    if not stocks:
        st.warning(f"{market} 종목 데이터를 가져올 수 없습니다.")
        return pd.DataFrame()

    return (
        pd.DataFrame(stocks)
        .drop_duplicates("ticker")
        .set_index("ticker")
    )


@st.cache_data(ttl=3600)
def get_ohlcv(ticker: str, start: str, end: str) -> pd.DataFrame:
    """
    개별 종목 OHLCV 조회
    반환 컬럼: 시가, 고가, 저가, 종가, 거래량 (한국어 통일)
    """
    try:
        df = fdr.DataReader(ticker, _to_fdr_date(start), _to_fdr_date(end))
        if df.empty:
            return pd.DataFrame()
        df = df.rename(columns={
            "Open": "시가",
            "High": "고가",
            "Low": "저가",
            "Close": "종가",
            "Volume": "거래량",
        })
        return df[["시가", "고가", "저가", "종가", "거래량"]]
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=3600)
def _get_integration(ticker: str) -> dict | None:
    """네이버 증권(m.stock.naver.com) 종목 통합 정보 API — 종목명 + 일별 수급(dealTrendInfos) 포함"""
    url = f"https://m.stock.naver.com/api/stock/{ticker}/integration"
    try:
        resp = requests.get(url, headers=_NAVER_HEADERS, timeout=5)
        return resp.json()
    except Exception:
        return None


@st.cache_data(ttl=86400)
def get_ticker_name(ticker: str, market: str = "KOSPI") -> str:
    """종목 코드 → 종목명 변환 (네이버 증권 통합 API)"""
    data = _get_integration(ticker)
    if data and data.get("stockName"):
        return data["stockName"]
    return ticker


@st.cache_data(ttl=3600)
def get_investor_flow(ticker: str) -> dict | None:
    """
    네이버 증권 통합 API의 dealTrendInfos(최근 거래일별 수급)에서
    최근 3일 기관/외국인 순매수 일수 집계
    반환: {"기관_순매수일": int, "외국인_순매수일": int}
    실패 시 None 반환 → 호출 측에서 수급 조건 스킵
    """
    data = _get_integration(ticker)
    if not data:
        return None

    deal_trends = data.get("dealTrendInfos") or []
    if not deal_trends:
        return None

    inst_days = 0
    foreign_days = 0
    for day in deal_trends[:3]:
        inst_val = _parse_naver_number(day.get("organPureBuyQuant") or "0")
        foreign_val = _parse_naver_number(day.get("foreignerPureBuyQuant") or "0")
        if inst_val > 0:
            inst_days += 1
        if foreign_val > 0:
            foreign_days += 1

    return {"기관_순매수일": inst_days, "외국인_순매수일": foreign_days}


@st.cache_data(ttl=1800)
def get_stock_news(ticker: str, count: int = 3) -> list[dict]:
    """
    네이버 증권(m.stock.naver.com) 종목 뉴스 API
    반환: [{"title": str, "date": str, "url": str}, ...]
    실패 시 빈 리스트 반환
    """
    url = f"https://m.stock.naver.com/api/news/stock/{ticker}?pageSize={count}"
    try:
        resp = requests.get(url, headers=_NAVER_HEADERS, timeout=5)
        groups = resp.json()
        items = groups[0].get("items", []) if groups else []

        results = []
        for item in items[:count]:
            raw_dt = item.get("datetime", "")
            date = (
                f"{raw_dt[:4]}-{raw_dt[4:6]}-{raw_dt[6:8]} {raw_dt[8:10]}:{raw_dt[10:12]}"
                if len(raw_dt) >= 12 else raw_dt
            )
            results.append({
                "title": item.get("title", ""),
                "date": date,
                "url": item.get("mobileNewsUrl", ""),
            })

        return results
    except Exception:
        return []


@st.cache_data(ttl=3600)
def get_market_direction(date: str) -> dict[str, object]:
    """KOSPI 지수 MA20 대비 현재 위치 반환"""
    start = (datetime.strptime(date, "%Y%m%d") - timedelta(days=60)).strftime("%Y%m%d")
    df = get_ohlcv("^KS11", start, date)
    if len(df) < 20:
        return {"trend": "unknown", "kospi": 0, "ma20": 0, "gap_pct": 0.0}
    kospi_raw = df["종가"].iloc[-1]
    ma20_raw  = df["종가"].rolling(20).mean().iloc[-1]
    if pd.isna(kospi_raw) or pd.isna(ma20_raw):
        return {"trend": "unknown", "kospi": 0, "ma20": 0, "gap_pct": 0.0}
    kospi = float(kospi_raw)
    ma20  = float(ma20_raw)
    gap_pct = round((kospi - ma20) / ma20 * 100, 2)
    return {
        "trend": "상승" if kospi >= ma20 else "하락",
        "kospi": int(kospi),
        "ma20": int(ma20),
        "gap_pct": gap_pct,
    }


def add_moving_averages(df: pd.DataFrame) -> pd.DataFrame:
    """MA5, MA20, MA60, MA120 컬럼 추가"""
    df = df.copy()
    df["MA5"] = df["종가"].rolling(window=5).mean()
    df["MA20"] = df["종가"].rolling(window=20).mean()
    df["MA60"] = df["종가"].rolling(window=60).mean()
    df["MA120"] = df["종가"].rolling(window=120).mean()
    return df


def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """RSI 컬럼 추가 (Wilder 방식, pandas-ta 불필요)"""
    df = df.copy()
    delta = df["종가"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss
    df["RSI"] = 100 - (100 / (1 + rs))
    return df


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """ATR 컬럼 추가 — 변동성 기반 익절/손절 계산 (pandas-ta 불필요)"""
    df = df.copy()
    high_low = df["고가"] - df["저가"]
    high_prev = (df["고가"] - df["종가"].shift()).abs()
    low_prev = (df["저가"] - df["종가"].shift()).abs()
    true_range = pd.concat([high_low, high_prev, low_prev], axis=1).max(axis=1)
    df["ATR"] = true_range.ewm(com=period - 1, min_periods=period).mean()
    return df
