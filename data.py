"""Fetch price + metadata. Universe via FinanceDataReader; prices/volume via yfinance."""

import time
from datetime import date, timedelta
from typing import Tuple

import numpy as np
import pandas as pd
import yfinance as yf
import FinanceDataReader as fdr

from universe import (
    KR_MIN_MARKET_CAP_KRW,
    KR_UNIVERSE_SIZE,
    US_DRIVERS,
    HISTORY_DAYS,
)


def _retry(fn, tries=3, delay=2):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(delay)
    raise last


def latest_trading_day_kr() -> date:
    """Use Samsung Electronics (005930) recent data to determine the latest KR trading day."""
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=14)
    df = _retry(lambda: fdr.DataReader("005930", start, end))
    if df is None or len(df) == 0:
        raise RuntimeError("Could not determine latest KR trading day from FDR")
    return df.index[-1].date()


def fetch_kr_universe(as_of: date) -> pd.DataFrame:
    """Get all KRX listed stocks with market cap, return top N above the cap floor.
    Returns DataFrame indexed by 6-digit code with columns: market, name, market_cap, volume, yf_ticker.
    """
    listing = _retry(lambda: fdr.StockListing("KRX"))
    # Expected columns include: Code, Name, Market, Marcap, Volume, Close, etc.
    if "Marcap" not in listing.columns:
        raise RuntimeError(f"FDR StockListing missing 'Marcap'. Got: {list(listing.columns)}")
    listing = listing.dropna(subset=["Marcap", "Code", "Market"])
    listing = listing[listing["Marcap"] >= KR_MIN_MARKET_CAP_KRW]
    # Exclude ETF/ETN/SPAC where possible (Market is KOSPI/KOSDAQ; ETFs often KONEX or named accordingly)
    listing = listing[listing["Market"].isin(["KOSPI", "KOSDAQ"])]
    listing = listing.sort_values("Marcap", ascending=False).head(KR_UNIVERSE_SIZE)

    def _yft(code, market):
        suffix = "KS" if market == "KOSPI" else "KQ"
        return f"{code}.{suffix}"

    yf_tickers = [_yft(c, m) for c, m in zip(listing["Code"], listing["Market"])]
    result = pd.DataFrame(
        {
            "market": listing["Market"].values,
            "name": listing["Name"].values,
            "market_cap": listing["Marcap"].astype(float).values,
            "volume": listing.get("Volume", pd.Series(np.nan, index=listing.index)).values,
            "yf_ticker": yf_tickers,
        },
        index=listing["Code"].values,
    )
    result.index.name = "code"
    return result


def fetch_prices(tickers, days: int = HISTORY_DAYS, with_volume: bool = False):
    """Yahoo Finance bulk download. Returns close_df, or (close_df, volume_df) if with_volume."""
    if not tickers:
        empty = pd.DataFrame()
        return (empty, empty) if with_volume else empty

    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=days)
    raw = _retry(
        lambda: yf.download(
            tickers,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            progress=False,
            auto_adjust=True,
            group_by="ticker",
            threads=True,
        ),
        tries=2,
    )

    if isinstance(raw.columns, pd.MultiIndex):
        level0 = list(raw.columns.levels[0])
        present = [t for t in tickers if t in level0]
        close = pd.concat({t: raw[t]["Close"] for t in present}, axis=1)
        volume = pd.concat({t: raw[t]["Volume"] for t in present}, axis=1)
    else:
        close = raw[["Close"]].rename(columns={"Close": tickers[0]})
        volume = raw[["Volume"]].rename(columns={"Volume": tickers[0]})

    close = close.dropna(axis=1, how="all")
    volume = volume.dropna(axis=1, how="all")
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    volume.index = pd.to_datetime(volume.index).tz_localize(None).normalize()
    close = close.sort_index()
    volume = volume.sort_index()
    return (close, volume) if with_volume else close


def daily_returns(prices: pd.DataFrame) -> pd.DataFrame:
    return prices.pct_change().dropna(how="all")


def fetch_us_and_kr_prices(kr_yf_tickers) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Returns (us_close, kr_close, kr_volume)."""
    us_close = fetch_prices(US_DRIVERS)
    kr_close, kr_volume = fetch_prices(list(kr_yf_tickers), with_volume=True)
    return us_close, kr_close, kr_volume
