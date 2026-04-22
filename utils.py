from datetime import datetime, timedelta

import FinanceDataReader as fdr
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

_NAVER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com",
}


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


def _parse_naver_number(text: str) -> int:
    cleaned = text.strip().replace(",", "").replace("+", "").replace("\xa0", "").replace(" ", "")
    try:
        return int(cleaned)
    except ValueError:
        return 0


@st.cache_data(ttl=3600)
def get_stock_listing(market: str = "KOSPI") -> pd.DataFrame:
    """
    네이버 금융 시가총액 순 페이지에서 종목 리스팅 (KRX API 불필요)
    시총 300억 미만 종목 도달 시 스캔 중단
    거래대금 = 현재가 × 거래량 (근사치)
    """
    sosok = "0" if market == "KOSPI" else "1"
    stocks: list[dict] = []
    MIN_CAP = 30_000_000_000  # 300억

    for page in range(1, 150):
        url = (
            f"https://finance.naver.com/sise/sise_market_sum.naver"
            f"?sosok={sosok}&page={page}"
        )
        try:
            resp = requests.get(url, headers=_NAVER_HEADERS, timeout=10)
            resp.encoding = "euc-kr"
            soup = BeautifulSoup(resp.text, "lxml")
        except Exception:
            break

        table = soup.find("table", class_="type_2")
        if not table:
            break

        found = False
        stop = False
        for row in table.find_all("tr"):
            cols = row.find_all("td")
            if len(cols) < 10:
                continue
            a_tag = cols[1].find("a")
            if not a_tag or "code=" not in a_tag.get("href", ""):
                continue

            ticker = a_tag["href"].split("code=")[-1].strip()
            name = a_tag.get_text(strip=True)
            close = _parse_naver_number(cols[2].get_text())
            market_cap = _parse_naver_number(cols[6].get_text()) * 100_000_000  # 억→원
            volume = _parse_naver_number(cols[9].get_text())
            trade_amount = close * volume  # 거래대금 근사치

            if market_cap < MIN_CAP:
                stop = True
                break

            stocks.append({
                "ticker": ticker,
                "name": name,
                "종가": close,
                "거래대금": trade_amount,
                "market_cap": market_cap,
                "거래량": volume,
            })
            found = True

        if stop or not found:
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


@st.cache_data(ttl=86400)
def get_ticker_name(ticker: str, market: str = "KOSPI") -> str:
    """종목 코드 → 종목명 변환 (네이버 금융)"""
    try:
        url = f"https://finance.naver.com/item/main.naver?code={ticker}"
        resp = requests.get(url, headers=_NAVER_HEADERS, timeout=5)
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "lxml")
        h1 = soup.find("div", class_="wrap_company")
        if h1:
            a = h1.find("a")
            if a:
                return a.get_text(strip=True)
        return ticker
    except Exception:
        return ticker


@st.cache_data(ttl=3600)
def get_investor_flow(ticker: str) -> dict | None:
    """
    네이버 금융 frgn.naver에서 기관/외국인 최근 3일 순매수 일수 스크래핑
    페이지 구조: type2 테이블 두 번째, col[5]=기관순매매량, col[6]=외국인순매매량
    반환: {"기관_순매수일": int, "외국인_순매수일": int}
    실패 시 None 반환 → 호출 측에서 수급 조건 스킵
    """
    url = f"https://finance.naver.com/item/frgn.naver?code={ticker}"
    try:
        resp = requests.get(url, headers=_NAVER_HEADERS, timeout=5)
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "lxml")

        type2_tables = soup.find_all("table", class_="type2")
        if len(type2_tables) < 2:
            return None
        table = type2_tables[1]  # 두 번째 type2 테이블이 수급 데이터

        inst_days = 0
        foreign_days = 0
        count = 0

        for row in table.find_all("tr"):
            cols = row.find_all("td")
            if len(cols) < 7:
                continue
            date_text = cols[0].get_text(strip=True)
            if not date_text or "." not in date_text:
                continue

            inst_val = _parse_naver_number(cols[5].get_text())
            foreign_val = _parse_naver_number(cols[6].get_text())

            if inst_val > 0:
                inst_days += 1
            if foreign_val > 0:
                foreign_days += 1

            count += 1
            if count >= 3:
                break

        if count == 0:
            return None

        return {"기관_순매수일": inst_days, "외국인_순매수일": foreign_days}
    except Exception:
        return None


@st.cache_data(ttl=1800)
def get_stock_news(ticker: str, count: int = 3) -> list[dict]:
    """
    네이버 금융 종목 뉴스 스크래핑
    반환: [{"title": str, "date": str, "url": str}, ...]
    실패 시 빈 리스트 반환
    """
    url = f"https://finance.naver.com/item/news_news.naver?code={ticker}&page=1"
    try:
        resp = requests.get(url, headers=_NAVER_HEADERS, timeout=5)
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "lxml")
        table = soup.find("table", class_="type5")
        if table is None:
            return []

        results = []
        for row in table.find_all("tr"):
            title_td = row.find("td", class_="title")
            date_td = row.find("td", class_="date")
            if not title_td or not date_td:
                continue
            a_tag = title_td.find("a")
            if not a_tag:
                continue
            title = a_tag.get_text(strip=True)
            href = a_tag.get("href", "")
            date = date_td.get_text(strip=True)
            if href.startswith("/"):
                href = "https://finance.naver.com" + href
            results.append({"title": title, "date": date, "url": href})
            if len(results) >= count:
                break

        return results
    except Exception:
        return []


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
