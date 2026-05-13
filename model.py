"""Conditional uplift model: predict next-day KR returns from last-night US moves."""

from dataclasses import dataclass
from typing import Dict, List

import numpy as np
import pandas as pd

from universe import (
    MIN_VOLUME_RATIO,
    ROLLING_WINDOW_DAYS,
    TOP_K_DRIVERS_PER_STOCK,
    TOP_K_PICKS,
    US_MOVE_THRESHOLD_PCT,
)


@dataclass
class Pick:
    ticker6: str           # 6-digit Korean code
    name: str
    market_cap: float      # KRW
    yf_ticker: str
    expected_return: float # decimal
    volume_ratio: float    # recent / 20d median
    drivers: List[tuple]   # [(us_ticker, beta, contribution_pct), ...]


def align_lead_lag(us_returns: pd.DataFrame, kr_returns: pd.DataFrame) -> pd.DataFrame:
    """Build a panel where, for each KR trading day d, we attach the most recent US session
    return that closed BEFORE the KR session opened.
    US close (4pm ET) is during the KR pre-open of next day. So:
        kr_return[d] is regressed on us_return[d-1] (US session ending the night before KR d).
    yfinance dates: US returns indexed by US calendar date. KR returns indexed by KR calendar date.
    We forward-fill US returns to align with KR trading days.
    """
    us = us_returns.copy()
    kr = kr_returns.copy()
    full_index = us.index.union(kr.index).sort_values()
    us_aligned = us.reindex(full_index).ffill()
    us_lag = us_aligned.shift(1)
    panel = kr.join(us_lag, how="inner", lsuffix="_kr", rsuffix="_us")
    return panel, kr, us_lag


def compute_betas(kr_returns: pd.DataFrame, us_returns: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """For each KR stock, regress kr_t on us_{t-1} (univariate per US driver).
    Returns dict keyed by KR yf_ticker, each value a DataFrame with rows indexed by US ticker
    and columns: beta, corr, n.
    """
    _, kr, us_lag = align_lead_lag(us_returns, kr_returns)
    common = kr.index.intersection(us_lag.index)
    if ROLLING_WINDOW_DAYS and len(common) > ROLLING_WINDOW_DAYS:
        common = common[-ROLLING_WINDOW_DAYS:]
    kr = kr.loc[common]
    us_lag = us_lag.loc[common]

    out: Dict[str, pd.DataFrame] = {}
    us_cols = us_lag.columns.tolist()
    us_var = us_lag.var()

    for kr_t in kr.columns:
        kr_s = kr[kr_t]
        valid = kr_s.notna()
        if valid.sum() < 60:
            continue
        rows = []
        for us_t in us_cols:
            us_s = us_lag[us_t]
            mask = valid & us_s.notna()
            n = int(mask.sum())
            if n < 60:
                continue
            x = us_s[mask].values
            y = kr_s[mask].values
            xv = x.var()
            if xv == 0:
                continue
            cov = np.cov(x, y, ddof=0)[0, 1]
            beta = cov / xv
            corr = np.corrcoef(x, y)[0, 1] if y.std() > 0 else 0.0
            rows.append({"us": us_t, "beta": float(beta), "corr": float(corr), "n": n})
        if rows:
            df = pd.DataFrame(rows).set_index("us")
            out[kr_t] = df
    return out


def last_us_moves(us_returns: pd.DataFrame) -> pd.Series:
    """Return the most recent (last row) US returns as a Series, in percent units (e.g. 0.023 = +2.3%)."""
    return us_returns.iloc[-1].dropna()


def score_universe(
    betas: Dict[str, pd.DataFrame],
    last_us: pd.Series,
    meta: pd.DataFrame,
    recent_volume: pd.DataFrame,
) -> List[Pick]:
    """Score each KR stock and return ranked Pick list."""
    movers = last_us[last_us.abs() >= (US_MOVE_THRESHOLD_PCT / 100.0)]
    if movers.empty:
        return []

    yf_to_ticker6 = {row.yf_ticker: t for t, row in meta.iterrows()}

    vol_ratio = {}
    if not recent_volume.empty:
        median20 = recent_volume.tail(21).iloc[:-1].median()  # 20 days excluding latest
        latest = recent_volume.iloc[-1]
        ratio = (latest / median20).replace([np.inf, -np.inf], np.nan).dropna()
        vol_ratio = ratio.to_dict()

    picks: List[Pick] = []
    for yf_t, df in betas.items():
        ticker6 = yf_to_ticker6.get(yf_t)
        if ticker6 is None or ticker6 not in meta.index:
            continue
        row = meta.loc[ticker6]

        # pick the top-K most strongly correlated US drivers for this stock
        top = df.reindex(df["corr"].abs().sort_values(ascending=False).index).head(
            TOP_K_DRIVERS_PER_STOCK
        )
        # contribution from each driver, restricted to drivers that actually moved last session
        driver_rows = []
        expected = 0.0
        for us_t, r in top.iterrows():
            us_move = movers.get(us_t, 0.0)
            if us_move == 0.0:
                continue
            contrib = r["beta"] * us_move
            expected += contrib
            driver_rows.append((us_t, float(r["beta"]), float(contrib)))

        if not driver_rows:
            continue

        vr = vol_ratio.get(ticker6, np.nan)
        picks.append(
            Pick(
                ticker6=ticker6,
                name=row["name"],
                market_cap=float(row["market_cap"]),
                yf_ticker=yf_t,
                expected_return=float(expected),
                volume_ratio=float(vr) if vr == vr else float("nan"),
                drivers=driver_rows,
            )
        )

    # rank: prioritize positive expected return, then by abs magnitude × (volume bonus)
    def sort_key(p: Pick):
        vol_bonus = max(p.volume_ratio - 1.0, 0.0) if p.volume_ratio == p.volume_ratio else 0.0
        return -(p.expected_return * (1.0 + vol_bonus))

    picks.sort(key=sort_key)

    # Filter: require positive expected return and (if available) volume_ratio >= threshold OR NaN volume
    filtered = []
    for p in picks:
        if p.expected_return <= 0:
            continue
        if p.volume_ratio == p.volume_ratio and p.volume_ratio < MIN_VOLUME_RATIO:
            continue
        filtered.append(p)
        if len(filtered) >= TOP_K_PICKS:
            break

    # If filter is too strict (no candidates), fall back to top by expected return regardless of volume
    if not filtered:
        fallback = [p for p in picks if p.expected_return > 0][:TOP_K_PICKS]
        return fallback
    return filtered
