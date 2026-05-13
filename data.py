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


def fetch_kr_full_listing() -> pd.DataFrame:
    """Fetch the ENTIRE KOSPI+KOSDAQ listing from FDR — all listed stocks regardless of market cap.
    Returned DataFrame is indexed by 6-digit Code; columns preserved as FDR returns them
    (typically: Name, Market, Marcap, Volume, Close, Changes, etc.).
    """
    listing = _retry(lambda: fdr.StockListing("KRX"))
    if "Code" not in listing.columns:
        raise RuntimeError(f"FDR StockListing missing 'Code'. Got: {list(listing.columns)}")
    listing = listing.dropna(subset=["Code"])
    # Normalize Code as 6-digit string
    listing["Code"] = listing["Code"].astype(str).str.zfill(6)
    # Keep only KOSPI/KOSDAQ (drop KONEX, ETFs etc.)
    if "Market" in listing.columns:
        listing = listing[listing["Market"].isin(["KOSPI", "KOSDAQ"])]
    listing = listing.drop_duplicates(subset=["Code"]).set_index("Code")
    listing.index.name = "code"
    if "Marcap" in listing.columns:
        listing = listing.sort_values("Marcap", ascending=False)
    return listing


def filter_to_picking_universe(full_listing: pd.DataFrame) -> pd.DataFrame:
    """Apply 시총 floor (and optional size cap) to derive the picking universe.
    Returns DataFrame indexed by code with columns: market, name, market_cap, volume, yf_ticker.
    """
    df = full_listing.copy()
    if "Marcap" not in df.columns:
        raise RuntimeError("Full listing has no Marcap column")
    df = df.dropna(subset=["Marcap"])
    df = df[df["Marcap"] >= KR_MIN_MARKET_CAP_KRW]
    df = df.sort_values("Marcap", ascending=False)
    if KR_UNIVERSE_SIZE is not None:
        df = df.head(KR_UNIVERSE_SIZE)

    def _yft(code, market):
        suffix = "KS" if market == "KOSPI" else "KQ"
        return f"{code}.{suffix}"

    yf_tickers = [_yft(c, m) for c, m in zip(df.index, df["Market"])]
    out = pd.DataFrame(
        {
            "market": df["Market"].values,
            "name": df["Name"].values,
            "market_cap": df["Marcap"].astype(float).values,
            "volume": df.get("Volume", pd.Series(np.nan, index=df.index)).values,
            "yf_ticker": yf_tickers,
        },
        index=df.index,
    )
    out.index.name = "code"
    return out


def fetch_kr_universe(as_of: date) -> pd.DataFrame:
    """Backward-compatible helper: full listing → picking universe."""
    return filter_to_picking_universe(fetch_kr_full_listing())


def _fetch_prices_one_chunk(tickers, days: int):
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
        close = pd.concat({t: raw[t]["Close"] for t in present}, axis=1) if present else pd.DataFrame()
        volume = pd.concat({t: raw[t]["Volume"] for t in present}, axis=1) if present else pd.DataFrame()
    else:
        # Single ticker case
        close = raw[["Close"]].rename(columns={"Close": tickers[0]}) if "Close" in raw.columns else pd.DataFrame()
        volume = raw[["Volume"]].rename(columns={"Volume": tickers[0]}) if "Volume" in raw.columns else pd.DataFrame()
    for df in (close, volume):
        if not df.empty:
            df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return close, volume


def fetch_prices(tickers, days: int = HISTORY_DAYS, with_volume: bool = False, chunk_size: int = 100):
    """Yahoo Finance bulk download with chunking. Returns close_df, or (close_df, volume_df) if with_volume."""
    if not tickers:
        empty = pd.DataFrame()
        return (empty, empty) if with_volume else empty

    tickers = list(tickers)
    close_parts, volume_parts = [], []
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i : i + chunk_size]
        try:
            c, v = _fetch_prices_one_chunk(chunk, days)
            if not c.empty:
                close_parts.append(c)
            if not v.empty:
                volume_parts.append(v)
        except Exception as e:
            print(f"[fetch_prices] chunk {i // chunk_size} failed: {e}")
            continue

    close = pd.concat(close_parts, axis=1).sort_index() if close_parts else pd.DataFrame()
    volume = pd.concat(volume_parts, axis=1).sort_index() if volume_parts else pd.DataFrame()
    close = close.loc[:, ~close.columns.duplicated()]
    volume = volume.loc[:, ~volume.columns.duplicated()]
    close = close.dropna(axis=1, how="all")
    volume = volume.dropna(axis=1, how="all")
    return (close, volume) if with_volume else close


def daily_returns(prices: pd.DataFrame) -> pd.DataFrame:
    return prices.pct_change().dropna(how="all")


def fetch_us_and_kr_prices(kr_yf_tickers) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Returns (us_close, kr_close, kr_volume)."""
    us_close = fetch_prices(US_DRIVERS)
    kr_close, kr_volume = fetch_prices(list(kr_yf_tickers), with_volume=True)
    return us_close, kr_close, kr_volume
