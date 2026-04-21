from datetime import datetime, timedelta

import FinanceDataReader as fdr
import pandas as pd
import pandas_ta as ta
import streamlit as st


def get_last_trading_date() -> str:
    """직전 거래일 반환 (장 마감 15:30 기준, YYYYMMDD 포맷)"""
    now = datetime.now()
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


@st.cache_data(ttl=3600)
def get_stock_listing(market: str = "KOSPI") -> pd.DataFrame:
    """
    시장 전체 종목 정보 조회 (오늘 기준 스냅샷)
    반환 컬럼: name, 시가, 고가, 저가, 종가, 거래량, 거래대금, market_cap
    인덱스: 종목코드
    """
    try:
        df = fdr.StockListing(market)
        if df.empty:
            st.warning(f"{market} 종목 데이터를 가져올 수 없습니다.")
            return pd.DataFrame()
        df = df.rename(columns={
            "Code": "ticker",
            "Name": "name",
            "Marcap": "market_cap",
            "Open": "시가",
            "High": "고가",
            "Low": "저가",
            "Close": "종가",
            "Volume": "거래량",
            "Amount": "거래대금",
        })
        df = df.set_index("ticker")
        return df
    except Exception as e:
        st.warning(f"{market} 데이터 조회 실패: {e}")
        return pd.DataFrame()


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


@st.cache_data(ttl=86400)
def get_ticker_name(ticker: str, market: str = "KOSPI") -> str:
    """종목 코드 → 종목명 변환"""
    try:
        df = fdr.StockListing(market)
        row = df[df["Code"] == ticker]
        if not row.empty:
            return row.iloc[0]["Name"]
        # KOSDAQ도 확인
        df2 = fdr.StockListing("KOSDAQ")
        row2 = df2[df2["Code"] == ticker]
        if not row2.empty:
            return row2.iloc[0]["Name"]
        return ticker
    except Exception:
        return ticker


def add_moving_averages(df: pd.DataFrame) -> pd.DataFrame:
    """MA5, MA20 컬럼 추가"""
    df = df.copy()
    df["MA5"] = df["종가"].rolling(window=5).mean()
    df["MA20"] = df["종가"].rolling(window=20).mean()
    return df


def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """RSI 컬럼 추가"""
    df = df.copy()
    df["RSI"] = ta.rsi(df["종가"], length=period)
    return df


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """ATR 컬럼 추가 — 변동성 기반 익절/손절 계산에 사용"""
    df = df.copy()
    df["ATR"] = ta.atr(df["고가"], df["저가"], df["종가"], length=period)
    return df
