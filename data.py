"""Fetch price and metadata. Universe + names + market cap via pykrx; prices via yfinance."""

import time
from datetime import date, timedelta
from typing import Tuple

import numpy as np
import pandas as pd
import yfinance as yf
from pykrx import stock

from universe import (
    KR_MIN_MARKET_CAP_KRW,
    KR_UNIVERSE_SIZE,
    US_DRIVERS,
    HISTORY_DAYS,
)


def _retry(fn, tries=3, delay=2):
    last_err = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if i < tries - 1:
                time.sleep(delay)
    raise last_err


def latest_trading_day_kr() -> date:
    """Most recent date for which KRX has data (handles weekends/holidays)."""
    today = date.today()
    for delta in range(0, 10):
        d = today - timedelta(days=delta)
        ds = d.strftime("%Y%m%d")
        try:
            df = stock.get_market_cap_by_ticker(ds, market="KOSPI")
            if df is not None and len(df) > 0 and df["시가총액"].sum() > 0:
                return d
        except Exception:
            continue
    raise RuntimeError("Could not determine latest KR trading day")


def fetch_kr_universe(as_of: date) -> pd.DataFrame:
    """Return DataFrame indexed by 6-digit ticker with columns: market, name, market_cap, volume, yf_ticker."""
    as_of_str = as_of.strftime("%Y%m%d")

    kospi_cap = _retry(lambda: stock.get_market_cap_by_ticker(as_of_str, market="KOSPI"))
    kosdaq_cap = _retry(lambda: stock.get_market_cap_by_ticker(as_of_str, market="KOSDAQ"))
    kospi_cap = kospi_cap.assign(market="KOSPI")
    kosdaq_cap = kosdaq_cap.assign(market="KOSDAQ")

    cap = pd.concat([kospi_cap, kosdaq_cap])
    cap = cap[cap["시가총액"] >= KR_MIN_MARKET_CAP_KRW]
    cap = cap.sort_values("시가총액", ascending=False).head(KR_UNIVERSE_SIZE)

    cap["name"] = [stock.get_market_ticker_name(t) for t in cap.index]
    cap["market_cap"] = cap["시가총액"]
    cap["volume"] = cap["거래량"]
    cap["yf_ticker"] = [
        f"{t}.{'KS' if m == 'KOSPI' else 'KQ'}" for t, m in zip(cap.index, cap["market"])
    ]
    return cap[["market", "name", "market_cap", "volume", "yf_ticker"]]


def fetch_kr_recent_volume(tickers, as_of: date, lookback_days: int = 30) -> pd.DataFrame:
    """Return DataFrame indexed by date with ticker columns of daily trading volume."""
    end = as_of
    start = as_of - timedelta(days=int(lookback_days * 1.8))
    start_s, end_s = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

    rows = []
    for t in tickers:
        try:
            df = _retry(lambda: stock.get_market_ohlcv_by_date(start_s, end_s, t))
            if df is None or df.empty:
                continue
            rows.append(df["거래량"].rename(t))
        except Exception:
            continue
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, axis=1).sort_index()


def fetch_prices(tickers, days: int = HISTORY_DAYS) -> pd.DataFrame:
    """Yahoo Finance bulk download of adjusted close. tickers is list of yfinance symbols.
    Returns wide DataFrame: date index, ticker columns."""
    if not tickers:
        return pd.DataFrame()
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=days)

    raw = yf.download(
        tickers,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        progress=False,
        auto_adjust=True,
        group_by="ticker",
        threads=True,
    )

    if isinstance(raw.columns, pd.MultiIndex):
        present = [t for t in tickers if t in raw.columns.levels[0]]
        close = pd.concat({t: raw[t]["Close"] for t in present}, axis=1)
    else:
        close = raw[["Close"]].rename(columns={"Close": tickers[0]})

    close = close.dropna(axis=1, how="all")
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    return close.sort_index()


def daily_returns(prices: pd.DataFrame) -> pd.DataFrame:
    return prices.pct_change().dropna(how="all")


def fetch_us_and_kr_prices(kr_yf_tickers) -> Tuple[pd.DataFrame, pd.DataFrame]:
    us = fetch_prices(US_DRIVERS)
    kr = fetch_prices(list(kr_yf_tickers))
    return us, kr
