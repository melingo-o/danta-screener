"""Fetch price + metadata. Universe via FinanceDataReader; OHLCV via yfinance."""

import time
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Tuple

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


OHLCV_FIELDS = ("Open", "High", "Low", "Close", "Volume")


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


def _fetch_kr_listing_pykrx() -> pd.DataFrame:
    """Full KOSPI+KOSDAQ listing via pykrx (queries KRX directly).

    Robust replacement for fdr.StockListing('KRX'), whose hosted marcap-cache CSV
    periodically returns HTTP 404. Returns the same schema downstream code expects:
    indexed by 6-digit code, columns Name/Market/Marcap/Volume/Close.
    """
    from pykrx import stock as krxstock

    today = date.today()
    # KRX publishes after close; walk back up to 10 days to land on a session w/ data
    for offset in range(0, 10):
        ds = (today - timedelta(days=offset)).strftime("%Y%m%d")
        rows = []
        try:
            for market in ("KOSPI", "KOSDAQ"):
                cap = krxstock.get_market_cap_by_ticker(ds, market=market)
                if cap is None or len(cap) == 0:
                    continue
                for ticker6, r in cap.iterrows():
                    t6 = str(ticker6).zfill(6)
                    rows.append(
                        {
                            "code": t6,
                            "Name": krxstock.get_market_ticker_name(t6),
                            "Market": market,
                            "Marcap": float(r.get("시가총액", 0) or 0),
                            "Volume": float(r.get("거래량", 0) or 0),
                            "Close": float(r.get("종가", 0) or 0),
                        }
                    )
            if not rows:
                continue
            df = pd.DataFrame(rows).drop_duplicates(subset=["code"]).set_index("code")
            df.index.name = "code"
            df = df[df["Marcap"] > 0].sort_values("Marcap", ascending=False)
            print(f"[fetch_kr_full_listing] pykrx: {len(df)} stocks for {ds}")
            return df
        except Exception as e:
            print(f"[fetch_kr_full_listing] pykrx {ds} failed: {e}")
            continue
    raise RuntimeError("pykrx KR listing fetch failed for all recent dates")


def _fetch_kr_listing_fdr_krx() -> pd.DataFrame:
    """Legacy path: fdr.StockListing('KRX'). Its hosted marcap-cache CSV started
    returning HTTP 404 (2026-06), so this is kept only as a middle fallback in case
    the cache is restored upstream."""
    listing = _retry(lambda: fdr.StockListing("KRX"))
    if "Code" not in listing.columns:
        raise RuntimeError(f"FDR StockListing missing 'Code'. Got: {list(listing.columns)}")
    listing = listing.dropna(subset=["Code"])
    listing["Code"] = listing["Code"].astype(str).str.zfill(6)
    if "Market" in listing.columns:
        listing = listing[listing["Market"].isin(["KOSPI", "KOSDAQ"])]
    listing = listing.drop_duplicates(subset=["Code"]).set_index("Code")
    listing.index.name = "code"
    if "Marcap" not in listing.columns:
        raise RuntimeError("FDR KRX listing has no Marcap column")
    return listing.sort_values("Marcap", ascending=False)


def _fetch_kr_listing_snapshot() -> pd.DataFrame:
    """Last-resort fallback: reuse the most recently committed universe.csv snapshot.

    Stock membership + market cap barely move day-to-day, so a slightly stale listing
    is fine for universe selection — OHLCV is still fetched fresh from yfinance. This
    keeps the morning screening alive through any upstream KRX/FDR outage.
    """
    path = Path(__file__).parent / "data" / "universe.csv"
    if not path.exists():
        raise RuntimeError("no committed universe.csv snapshot to fall back to")
    df = pd.read_csv(path, dtype={"code": str})
    if "code" not in df.columns:
        raise RuntimeError(f"snapshot missing 'code'. Got: {list(df.columns)}")
    df["code"] = df["code"].astype(str).str.zfill(6)
    df = df.drop_duplicates(subset=["code"]).set_index("code")
    df.index.name = "code"
    needed = {"Name", "Market", "Marcap"}
    missing = needed - set(df.columns)
    if missing:
        raise RuntimeError(f"snapshot missing columns: {missing}")
    df = df[df["Market"].isin(["KOSPI", "KOSDAQ"])]
    df = df.dropna(subset=["Marcap"]).sort_values("Marcap", ascending=False)
    as_of = df["snapshot_date"].iloc[0] if "snapshot_date" in df.columns and len(df) else "?"
    print(f"[fetch_kr_full_listing] SNAPSHOT fallback: {len(df)} stocks (as of {as_of})")
    return df


def fetch_kr_full_listing() -> pd.DataFrame:
    """Fetch the entire KOSPI+KOSDAQ listing (~2700 stocks).

    Layered for resilience — both upstream sources have failed simultaneously
    (FDR's marcap-cache CSV → HTTP 404; KRX rejecting datacenter IPs → empty body):
      1. pykrx (queries KRX directly — freshest when reachable)
      2. fdr.StockListing('KRX') legacy cache (if upstream restores it)
      3. last committed data/universe.csv snapshot (guaranteed; keeps the run alive)
    Indexed by 6-digit Code. Columns include Name, Market, Marcap, Volume, Close.
    """
    for name, fn in (
        ("pykrx", _fetch_kr_listing_pykrx),
        ("fdr-krx", _fetch_kr_listing_fdr_krx),
        ("snapshot", _fetch_kr_listing_snapshot),
    ):
        try:
            return fn()
        except Exception as e:
            print(f"[fetch_kr_full_listing] {name} failed: {type(e).__name__}: {e}")
    raise RuntimeError("all KR listing sources failed (pykrx, fdr-krx, snapshot)")


def filter_to_picking_universe(full_listing: pd.DataFrame) -> pd.DataFrame:
    """Apply 시총 floor (and optional size cap) to derive the picking universe."""
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


def _fetch_chunk(tickers, start: str, end: str) -> Dict[str, pd.DataFrame]:
    """Fetch one yfinance batch. Returns dict of {field: DataFrame}."""
    raw = _retry(
        lambda: yf.download(
            tickers,
            start=start,
            end=end,
            progress=False,
            auto_adjust=True,
            group_by="ticker",
            threads=True,
        ),
        tries=2,
    )
    out = {f: pd.DataFrame() for f in OHLCV_FIELDS}
    if isinstance(raw.columns, pd.MultiIndex):
        level0 = list(raw.columns.levels[0])
        present = [t for t in tickers if t in level0]
        if not present:
            return out
        for f in OHLCV_FIELDS:
            try:
                out[f] = pd.concat({t: raw[t][f] for t in present}, axis=1)
            except KeyError:
                pass
    else:
        for f in OHLCV_FIELDS:
            if f in raw.columns:
                out[f] = raw[[f]].rename(columns={f: tickers[0]})
    return out


def fetch_ohlcv(tickers, days: int = HISTORY_DAYS, chunk_size: int = 100) -> Dict[str, pd.DataFrame]:
    """Bulk yfinance OHLCV download with chunking.
    Returns dict {Open, High, Low, Close, Volume}, each a DataFrame indexed by date with ticker cols.
    """
    if not tickers:
        return {f: pd.DataFrame() for f in OHLCV_FIELDS}

    tickers = list(tickers)
    end = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    start = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")

    parts = {f: [] for f in OHLCV_FIELDS}
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i : i + chunk_size]
        try:
            chunk_data = _fetch_chunk(chunk, start, end)
            for f in OHLCV_FIELDS:
                if not chunk_data[f].empty:
                    parts[f].append(chunk_data[f])
        except Exception as e:
            print(f"[fetch_ohlcv] chunk {i // chunk_size} failed: {e}")
            continue

    out = {}
    for f, lst in parts.items():
        if not lst:
            out[f] = pd.DataFrame()
            continue
        df = pd.concat(lst, axis=1)
        df = df.loc[:, ~df.columns.duplicated()]
        df = df.dropna(axis=1, how="all")
        df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
        out[f] = df.sort_index()
    return out


def daily_returns(close: pd.DataFrame) -> pd.DataFrame:
    """Standard close-to-close pct change."""
    return close.pct_change().dropna(how="all")


def gap_returns(open_df: pd.DataFrame, close_df: pd.DataFrame) -> pd.DataFrame:
    """Overnight gap: (Open[t] - Close[t-1]) / Close[t-1]. Indexed by the open's date t.
    This is the target most relevant for 9:00 AM trading — captures the prev-close → open jump
    that overnight news (US session) drives.
    """
    cols = open_df.columns.intersection(close_df.columns)
    o = open_df[cols]
    c_prev = close_df[cols].shift(1)
    gap = (o - c_prev) / c_prev
    gap = gap.replace([np.inf, -np.inf], np.nan)
    return gap.dropna(how="all")
