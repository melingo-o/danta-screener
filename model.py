"""Conditional uplift model: predict next-day KR overnight gap from last-night US moves.

Tier 1 filters applied:
- min |corr| threshold (skip noisy driver/stock pairs)
- min observations (skip new listings / short history)
- volume ratio upper cap (skip event-driven outliers)
- transaction cost adjustment in ranking
- sector (primary-driver) diversification in final selection
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from universe import (
    MAX_ABS_BETA,
    MAX_VOLUME_RATIO_CAP,
    MIN_CORR_THRESHOLD,
    MIN_OBSERVATIONS,
    MIN_VOLUME_RATIO,
    ROLLING_WINDOW_DAYS,
    SECTOR_DIVERSIFY,
    TOP_K_DRIVERS_PER_STOCK,
    TOP_K_PICKS,
    TRANSACTION_COST_PCT,
    US_MOVE_THRESHOLD_PCT,
)


@dataclass
class Pick:
    ticker6: str
    name: str
    market_cap: float
    yf_ticker: str
    expected_return: float
    expected_return_net: float
    volume_ratio: float
    drivers: List[tuple]
    primary_driver: Optional[str] = None


def align_lead_lag(us_returns: pd.DataFrame, kr_returns: pd.DataFrame):
    us = us_returns.copy()
    kr = kr_returns.copy()
    full_index = us.index.union(kr.index).sort_values()
    us_aligned = us.reindex(full_index).ffill()
    us_lag = us_aligned.shift(1)
    panel = kr.join(us_lag, how="inner", lsuffix="_kr", rsuffix="_us")
    return panel, kr, us_lag


def compute_betas(
    kr_returns: pd.DataFrame,
    us_returns: pd.DataFrame,
    *,
    rolling_window: int = ROLLING_WINDOW_DAYS,
    min_obs: int = MIN_OBSERVATIONS,
    min_corr: float = MIN_CORR_THRESHOLD,
    max_abs_beta: float = MAX_ABS_BETA,
) -> Dict[str, pd.DataFrame]:
    """Vectorized univariate OLS per (KR stock, US driver). Filters by min_obs and min_corr.

    Same math as the original loop but uses numpy matrix ops:
      corr  = pandas pairwise-complete corr (handles NaN correctly)
      σ_y, σ_x = full-column std (good approximation when NaN patterns are sparse)
      β = corr × σ_y / σ_x
      n = pairwise non-NaN count
    """
    _, kr, us_lag = align_lead_lag(us_returns, kr_returns)
    common = kr.index.intersection(us_lag.index)
    if rolling_window and len(common) > rolling_window:
        common = common[-rolling_window:]
    kr = kr.loc[common]
    us_lag = us_lag.loc[common]

    if kr.empty or us_lag.empty:
        return {}

    M = kr.shape[1]
    K = us_lag.shape[1]
    kr_cols = list(kr.columns)
    us_cols = list(us_lag.columns)

    # Pairwise non-NaN counts (M, K)
    valid_kr = kr.notna().values
    valid_us = us_lag.notna().values
    ns_mat = (valid_kr[:, :, None] & valid_us[:, None, :]).sum(axis=0).astype(np.int32)

    # Cross-correlation matrix kr × us_lag (pairwise complete)
    combined = pd.concat([kr, us_lag], axis=1)
    corr_full = combined.corr().values
    cross_corr = corr_full[:M, M:]  # (M, K)

    # Standard deviations
    sigma_y = kr.std(ddof=0).values  # (M,)
    sigma_x = us_lag.std(ddof=0).values  # (K,)

    with np.errstate(invalid="ignore", divide="ignore"):
        betas_mat = cross_corr * (sigma_y[:, None] / np.where(sigma_x[None, :] > 0, sigma_x[None, :], np.nan))

    abs_corr = np.abs(cross_corr)
    abs_beta = np.abs(betas_mat)
    valid_pair = (
        (ns_mat >= min_obs)
        & (abs_corr >= min_corr)
        & (abs_beta <= max_abs_beta)
        & ~np.isnan(betas_mat)
        & ~np.isnan(cross_corr)
    )

    if not valid_pair.any():
        return {}

    # Build long-format DataFrame, then groupby
    ii, jj = np.where(valid_pair)
    long_df = pd.DataFrame({
        "kr_t": [kr_cols[i] for i in ii],
        "us": [us_cols[j] for j in jj],
        "beta": betas_mat[ii, jj],
        "corr": cross_corr[ii, jj],
        "n": ns_mat[ii, jj],
    })

    out: Dict[str, pd.DataFrame] = {}
    for kr_t, sub in long_df.groupby("kr_t", sort=False):
        out[kr_t] = sub.drop(columns="kr_t").set_index("us")
    return out


def last_us_moves(us_returns: pd.DataFrame) -> pd.Series:
    return us_returns.iloc[-1].dropna()


def _volume_ratio_map(recent_volume: pd.DataFrame) -> Dict[str, float]:
    if recent_volume is None or recent_volume.empty:
        return {}
    tail = recent_volume.tail(21)
    if len(tail) < 5:
        return {}
    median20 = tail.iloc[:-1].median()
    latest = tail.iloc[-1]
    ratio = (latest / median20).replace([np.inf, -np.inf], np.nan).dropna()
    return ratio.to_dict()


def score_universe(
    betas: Dict[str, pd.DataFrame],
    last_us: pd.Series,
    meta: pd.DataFrame,
    recent_volume: pd.DataFrame,
    *,
    top_k_picks: int = TOP_K_PICKS,
    top_k_drivers: int = TOP_K_DRIVERS_PER_STOCK,
    move_threshold_pct: float = US_MOVE_THRESHOLD_PCT,
    min_volume_ratio: float = MIN_VOLUME_RATIO,
    max_volume_ratio: float = MAX_VOLUME_RATIO_CAP,
    cost_pct: float = TRANSACTION_COST_PCT,
    sector_diversify: bool = SECTOR_DIVERSIFY,
) -> List[Pick]:
    """Build ranked Pick list with Tier 1 filters."""
    movers = last_us[last_us.abs() >= (move_threshold_pct / 100.0)]
    if movers.empty:
        return []

    yf_to_ticker6 = {row.yf_ticker: t for t, row in meta.iterrows()}
    vol_ratio = _volume_ratio_map(recent_volume)
    cost = cost_pct / 100.0

    picks: List[Pick] = []
    for yf_t, df in betas.items():
        ticker6 = yf_to_ticker6.get(yf_t)
        if ticker6 is None or ticker6 not in meta.index:
            continue
        row = meta.loc[ticker6]

        top = df.reindex(df["corr"].abs().sort_values(ascending=False).index).head(
            top_k_drivers
        )
        driver_rows = []
        expected = 0.0
        primary_driver = None
        primary_contrib = 0.0
        for us_t, r in top.iterrows():
            us_move = movers.get(us_t, 0.0)
            if us_move == 0.0:
                continue
            contrib = r["beta"] * us_move
            expected += contrib
            driver_rows.append((us_t, float(r["beta"]), float(contrib)))
            if abs(contrib) > abs(primary_contrib):
                primary_contrib = contrib
                primary_driver = us_t

        if not driver_rows:
            continue

        vr = vol_ratio.get(ticker6, float("nan"))
        picks.append(
            Pick(
                ticker6=ticker6,
                name=row["name"],
                market_cap=float(row["market_cap"]),
                yf_ticker=yf_t,
                expected_return=float(expected),
                expected_return_net=float(expected - cost),
                volume_ratio=float(vr) if vr == vr else float("nan"),
                drivers=driver_rows,
                primary_driver=primary_driver,
            )
        )

    # Rank by NET expected return × volume bonus (capped)
    def sort_key(p: Pick):
        vol_bonus = 0.0
        if p.volume_ratio == p.volume_ratio:  # not NaN
            vb = min(max(p.volume_ratio - 1.0, 0.0), max_volume_ratio - 1.0)
            vol_bonus = vb
        return -(p.expected_return_net * (1.0 + vol_bonus))

    picks.sort(key=sort_key)

    # Filter pipeline
    used_primary = set()
    filtered: List[Pick] = []
    for p in picks:
        if p.expected_return_net <= 0:
            continue
        if p.volume_ratio == p.volume_ratio:
            if p.volume_ratio < min_volume_ratio:
                continue
            if p.volume_ratio > max_volume_ratio:
                continue
        if sector_diversify and p.primary_driver in used_primary:
            continue
        filtered.append(p)
        if p.primary_driver:
            used_primary.add(p.primary_driver)
        if len(filtered) >= top_k_picks:
            break

    if filtered:
        return filtered

    # Fallback: drop sector & cost constraints, but keep vol cap and min_vol
    relaxed: List[Pick] = []
    used_primary = set()
    for p in picks:
        if p.expected_return <= 0:
            continue
        if p.volume_ratio == p.volume_ratio and p.volume_ratio > max_volume_ratio:
            continue
        relaxed.append(p)
        if len(relaxed) >= top_k_picks:
            break
    return relaxed
