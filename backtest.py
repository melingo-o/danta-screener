"""Walk-forward backtest of the gap-prediction strategy.

For each test_date d in the last TEST_DAYS KR trading days:
  1. Slice all data to [start, d) — no lookahead.
  2. Compute betas + score, get top-K picks.
  3. Look up the ACTUAL gap that realized on day d for each pick.
  4. Record predicted vs actual.

Reports:
  - Hit rate (% picks with actual_gap > 0)
  - Hit rate net of cost (% picks with actual_gap > TRANSACTION_COST_PCT/100)
  - Pearson(predicted, actual)
  - Strategy daily mean / Sharpe / Win rate / Cum return
  - Random baseline (100 iterations) for comparison
  - Parameter sweep across 5 settings

Universe / data caveat: uses today's KRX listing for as-of universe (mild survivor bias,
~minor for 시총 5000억+ which is stable; explicitly noted in QA report).
"""

import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

from data import (
    daily_returns,
    fetch_kr_full_listing,
    fetch_ohlcv,
    filter_to_picking_universe,
    gap_returns,
)
from model import compute_betas, score_universe
from universe import (
    HISTORY_DAYS,
    MAX_VOLUME_RATIO_CAP,
    MIN_CORR_THRESHOLD,
    MIN_OBSERVATIONS,
    MIN_VOLUME_RATIO,
    TOP_K_PICKS,
    TRANSACTION_COST_PCT,
    US_DRIVERS,
)

DATA_DIR = Path(__file__).parent / "data"
TEST_DAYS = 60                # backtest window (most recent N KR trading days)
RANDOM_ITER = 100             # Monte Carlo iterations for random baseline


# ---------- Walk-forward backtest ----------

def run_backtest(
    kr_gap: pd.DataFrame,
    us_ret: pd.DataFrame,
    kr_volume: pd.DataFrame,
    meta: pd.DataFrame,
    *,
    test_days: int = TEST_DAYS,
    name: str = "baseline",
    **score_kwargs,
) -> pd.DataFrame:
    """Returns DataFrame of (date, rank, ticker6, name, predicted, predicted_net, actual, vol_ratio)."""
    test_dates = list(kr_gap.index[-test_days:])
    records = []

    yf_to_code = {row.yf_ticker: code for code, row in meta.iterrows()}
    vol_by_code = kr_volume.rename(columns=yf_to_code)
    vol_by_code = vol_by_code[[c for c in vol_by_code.columns if c in meta.index]]

    for d in test_dates:
        kr_train = kr_gap.loc[kr_gap.index < d]
        us_train = us_ret.loc[us_ret.index < d]
        vol_train = vol_by_code.loc[vol_by_code.index < d]

        if len(kr_train) < MIN_OBSERVATIONS or us_train.empty:
            continue

        try:
            betas = compute_betas(
                kr_train, us_train,
                min_obs=score_kwargs.get("min_obs", MIN_OBSERVATIONS),
                min_corr=score_kwargs.get("min_corr", MIN_CORR_THRESHOLD),
            )
        except Exception as e:
            print(f"  [warn] {d.date()}: compute_betas failed: {e}")
            continue

        if not betas:
            continue

        last_us = us_train.iloc[-1].dropna()

        # Filter score_kwargs to only those accepted by score_universe
        sk = {k: v for k, v in score_kwargs.items()
              if k in ("top_k_picks", "top_k_drivers", "move_threshold_pct",
                       "min_volume_ratio", "max_volume_ratio", "cost_pct",
                       "sector_diversify")}
        picks = score_universe(betas, last_us, meta, vol_train, **sk)

        actual_row = kr_gap.loc[d] if d in kr_gap.index else None
        for rank, p in enumerate(picks, 1):
            actual = float(actual_row.get(p.yf_ticker)) if actual_row is not None else np.nan
            records.append({
                "date": d.date(),
                "rank": rank,
                "ticker6": p.ticker6,
                "name": p.name,
                "primary_driver": p.primary_driver,
                "predicted": float(p.expected_return),
                "predicted_net": float(p.expected_return_net),
                "actual": actual,
                "vol_ratio": float(p.volume_ratio) if p.volume_ratio == p.volume_ratio else np.nan,
            })

    df = pd.DataFrame(records)
    df["strategy_name"] = name
    return df


def summarize(df: pd.DataFrame, name: str, cost_pct: float = TRANSACTION_COST_PCT) -> Dict:
    """Return dict of summary metrics for a backtest result."""
    if df.empty:
        return {"strategy": name, "picks": 0, "note": "no picks generated"}

    df = df.dropna(subset=["actual"]).copy()
    cost = cost_pct / 100.0

    n = len(df)
    hit = (df["actual"] > 0).mean()
    hit_net = (df["actual"] > cost).mean()
    mean_pred = df["predicted"].mean()
    mean_act = df["actual"].mean()
    corr = df[["predicted", "actual"]].corr().iloc[0, 1] if n >= 5 else float("nan")
    mae = (df["actual"] - df["predicted"]).abs().mean()

    daily = df.groupby("date")["actual"].mean()
    n_days = len(daily)
    daily_mean = daily.mean() if n_days else float("nan")
    daily_std = daily.std() if n_days > 1 else float("nan")
    sharpe = (daily_mean / daily_std * np.sqrt(252)) if (daily_std and daily_std > 0) else float("nan")
    win_rate = (daily > 0).mean() if n_days else float("nan")
    cum = float((1 + daily).prod() - 1) if n_days else float("nan")
    cum_net = float((1 + (daily - cost)).prod() - 1) if n_days else float("nan")

    return {
        "strategy": name,
        "picks": n,
        "days": n_days,
        "hit_rate": hit,
        "hit_rate_net": hit_net,
        "mean_predicted_pct": mean_pred * 100,
        "mean_actual_pct": mean_act * 100,
        "corr_pred_actual": corr,
        "mae_pct": mae * 100,
        "daily_mean_pct": daily_mean * 100,
        "daily_std_pct": daily_std * 100,
        "annualized_sharpe": sharpe,
        "win_rate_days": win_rate,
        "cum_return_gross_pct": cum * 100,
        "cum_return_net_pct": cum_net * 100,
    }


def random_baseline(
    kr_gap: pd.DataFrame,
    meta: pd.DataFrame,
    *,
    test_days: int = TEST_DAYS,
    iterations: int = RANDOM_ITER,
    n_picks: int = TOP_K_PICKS,
    seed: int = 42,
) -> Dict:
    """Monte Carlo: pick n_picks random stocks each day. Returns aggregated metrics."""
    rng = np.random.default_rng(seed)
    test_dates = list(kr_gap.index[-test_days:])
    universe = list(set(meta["yf_ticker"]) & set(kr_gap.columns))
    if len(universe) < n_picks:
        return {"strategy": "random", "note": "universe too small"}

    daily_returns_all = []
    cum_returns_all = []
    hit_rates = []
    for _ in range(iterations):
        daily = []
        hits = []
        for d in test_dates:
            if d not in kr_gap.index:
                continue
            sample = rng.choice(universe, size=n_picks, replace=False)
            actuals = kr_gap.loc[d, sample].dropna()
            if len(actuals) == 0:
                continue
            daily.append(actuals.mean())
            hits.extend((actuals > 0).tolist())
        if daily:
            d_series = pd.Series(daily)
            daily_returns_all.append(d_series.mean())
            cum_returns_all.append((1 + d_series).prod() - 1)
            hit_rates.append(np.mean(hits) if hits else float("nan"))

    return {
        "strategy": "random_baseline",
        "iterations": iterations,
        "mean_daily_return_pct": float(np.mean(daily_returns_all) * 100),
        "p2.5_daily_return_pct": float(np.percentile(daily_returns_all, 2.5) * 100),
        "p97.5_daily_return_pct": float(np.percentile(daily_returns_all, 97.5) * 100),
        "mean_cum_return_pct": float(np.mean(cum_returns_all) * 100),
        "mean_hit_rate": float(np.mean(hit_rates)),
    }


def universe_baseline(
    kr_gap: pd.DataFrame,
    meta: pd.DataFrame,
    *,
    test_days: int = TEST_DAYS,
) -> Dict:
    """Equal-weight all stocks in universe — pure 'market beta' of our picking universe."""
    universe_cols = list(set(meta["yf_ticker"]) & set(kr_gap.columns))
    sub = kr_gap[universe_cols].iloc[-test_days:]
    daily = sub.mean(axis=1)
    return {
        "strategy": "universe_equal_weight",
        "days": len(daily),
        "daily_mean_pct": float(daily.mean() * 100),
        "win_rate_days": float((daily > 0).mean()),
        "cum_return_pct": float(((1 + daily).prod() - 1) * 100),
    }


def main():
    print("=" * 60)
    print("WALK-FORWARD BACKTEST")
    print("=" * 60)

    # ---- 1. Fetch data ----
    print("\n[1/4] Fetching data...")
    full = fetch_kr_full_listing()
    print(f"  full listing: {len(full)} stocks")
    meta = filter_to_picking_universe(full)
    print(f"  picking universe (시총 ≥ 5000억): {len(meta)} stocks")

    us = fetch_ohlcv(US_DRIVERS)
    kr = fetch_ohlcv(meta["yf_ticker"].tolist())
    print(f"  US close: {us['Close'].shape}, KR open: {kr['Open'].shape}, KR close: {kr['Close'].shape}")

    if us["Close"].empty or kr["Open"].empty:
        raise RuntimeError("Empty price data")

    us_ret = daily_returns(us["Close"])
    kr_gap = gap_returns(kr["Open"], kr["Close"])
    print(f"  US returns: {us_ret.shape}, KR gap returns: {kr_gap.shape}")

    # Drop columns with too many NaN (likely delisted / new listing within history)
    kr_gap_clean = kr_gap.dropna(thresh=int(len(kr_gap) * 0.7), axis=1)
    print(f"  KR gap after dropping sparse columns: {kr_gap_clean.shape}")
    meta = meta[meta["yf_ticker"].isin(kr_gap_clean.columns)]
    print(f"  meta after filter: {len(meta)}")

    # ---- 2. Strategy sweep ----
    print(f"\n[2/4] Running walk-forward over last {TEST_DAYS} trading days...")

    strategies = {
        "tier1": dict(min_corr=0.15, min_obs=120, max_volume_ratio=10.0, sector_diversify=True),
        "no_corr_filter": dict(min_corr=0.0, min_obs=120, max_volume_ratio=10.0, sector_diversify=True),
        "tight_corr": dict(min_corr=0.25, min_obs=120, max_volume_ratio=10.0, sector_diversify=True),
        "no_vol_cap": dict(min_corr=0.15, min_obs=120, max_volume_ratio=1e9, sector_diversify=True),
        "no_sector_cap": dict(min_corr=0.15, min_obs=120, max_volume_ratio=10.0, sector_diversify=False),
    }

    all_picks_df = []
    summaries = []

    for name, kwargs in strategies.items():
        print(f"  → {name}: {kwargs}")
        df = run_backtest(kr_gap_clean, us_ret, kr["Volume"], meta, name=name, **kwargs)
        all_picks_df.append(df)
        s = summarize(df, name)
        summaries.append(s)

    # ---- 3. Baselines ----
    print(f"\n[3/4] Computing baselines...")
    rand = random_baseline(kr_gap_clean, meta, test_days=TEST_DAYS, iterations=RANDOM_ITER)
    univ = universe_baseline(kr_gap_clean, meta, test_days=TEST_DAYS)
    summaries.append(rand)
    summaries.append(univ)

    # ---- 4. Save results ----
    print(f"\n[4/4] Saving results...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    detail_df = pd.concat(all_picks_df, ignore_index=True) if all_picks_df else pd.DataFrame()
    detail_path = DATA_DIR / f"backtest_picks_{datetime.now().strftime('%Y%m%d')}.csv"
    if not detail_df.empty:
        detail_df.to_csv(detail_path, index=False, encoding="utf-8-sig")
        print(f"  detail: {detail_path} ({len(detail_df)} rows)")

    summary_df = pd.DataFrame(summaries)
    summary_path = DATA_DIR / f"backtest_summary_{datetime.now().strftime('%Y%m%d')}.csv"
    summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")
    print(f"  summary: {summary_path}")

    # ---- Print report ----
    print("\n" + "=" * 60)
    print("BACKTEST SUMMARY")
    print("=" * 60)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)
    pd.set_option("display.float_format", lambda v: f"{v:.3f}" if isinstance(v, float) else str(v))
    print(summary_df.to_string(index=False))
    print()

    # Quick verdict
    tier1 = next((s for s in summaries if s.get("strategy") == "tier1"), None)
    if tier1 and "daily_mean_pct" in tier1:
        print("\nKey numbers (tier1 strategy):")
        print(f"  - Picks generated: {tier1['picks']}")
        print(f"  - Hit rate (gross > 0): {tier1.get('hit_rate', 0):.1%}")
        print(f"  - Hit rate (after 0.5% cost): {tier1.get('hit_rate_net', 0):.1%}")
        print(f"  - Predicted vs Actual correlation: {tier1.get('corr_pred_actual', 0):.3f}")
        print(f"  - Daily mean gross: {tier1['daily_mean_pct']:.3f}%")
        print(f"  - Daily mean net (after cost): {(tier1['daily_mean_pct'] - TRANSACTION_COST_PCT):.3f}%")
        print(f"  - Cumulative gross: {tier1.get('cum_return_gross_pct', 0):.2f}%")
        print(f"  - Cumulative net:   {tier1.get('cum_return_net_pct', 0):.2f}%")
        print(f"  - vs random mean cum: {rand.get('mean_cum_return_pct', 0):.2f}%")
        print(f"  - vs universe equal-weight cum: {univ.get('cum_return_pct', 0):.2f}%")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
