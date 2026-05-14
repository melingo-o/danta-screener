"""Build daily technical indicators (SMA20, RSI14, 20-day high, 20-day median volume)
from yfinance OHLCV. Saves to data/daily_indicators.csv for use by intraday_scanner.

Only stocks meeting a market cap floor are included, to keep the file small.
"""

from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd

INDICATORS_CSV = Path("data/daily_indicators.csv")
MIN_CAP_FOR_INDICATORS = 200_000_000_000  # 2,000억 — keep some headroom below scanner floor


def rsi(close: pd.Series, period: int = 14) -> float:
    """Wilder's RSI for the LAST value of a close series."""
    if close.dropna().shape[0] < period + 1:
        return float("nan")
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_series = 100 - 100 / (1 + rs)
    val = rsi_series.iloc[-1]
    return float(val) if val == val else float("nan")


def build_indicators(meta: pd.DataFrame, kr_close: pd.DataFrame, kr_volume: pd.DataFrame) -> pd.DataFrame:
    """Compute per-stock indicators. Returns DataFrame indexed by ticker6.

    meta: DataFrame from filter_to_picking_universe (index = 6-digit code, has yf_ticker/name/market_cap)
    kr_close: DataFrame of daily Close, columns = yf_tickers (.KS/.KQ)
    kr_volume: same shape, daily Volume
    """
    rows = []
    yf_to_code = {row.yf_ticker: code for code, row in meta.iterrows()}

    for yf_t in kr_close.columns:
        code = yf_to_code.get(yf_t)
        if code is None:
            continue
        if code not in meta.index:
            continue
        if float(meta.loc[code, "market_cap"]) < MIN_CAP_FOR_INDICATORS:
            continue

        close = kr_close[yf_t].dropna()
        vol = kr_volume[yf_t].dropna() if yf_t in kr_volume.columns else pd.Series(dtype=float)
        if len(close) < 21:
            continue

        sma20 = close.tail(20).mean()
        high20 = close.tail(20).max()
        prev_close = close.iloc[-1]
        rsi14 = rsi(close, 14)
        vol_median20 = float(vol.tail(20).median()) if len(vol) >= 5 else float("nan")

        rows.append({
            "ticker6": code,
            "name": meta.loc[code, "name"],
            "market_cap": float(meta.loc[code, "market_cap"]),
            "sma20": round(float(sma20), 2),
            "high20": round(float(high20), 2),
            "prev_close": round(float(prev_close), 2),
            "rsi14": round(float(rsi14), 2) if rsi14 == rsi14 else "",
            "vol_median20": round(vol_median20, 0) if vol_median20 == vol_median20 else "",
        })

    df = pd.DataFrame(rows)
    return df


def save_indicators(df: pd.DataFrame) -> None:
    INDICATORS_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(INDICATORS_CSV, index=False, encoding="utf-8-sig")
    print(f"[indicators] saved {len(df)} rows → {INDICATORS_CSV}")
