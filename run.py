"""Main entry point: fetch data → compute → send Telegram."""

import os
import sys
import traceback
from pathlib import Path

import pandas as pd

from data import (
    daily_returns,
    fetch_kr_full_listing,
    fetch_prices,
    filter_to_picking_universe,
    latest_trading_day_kr,
)
from model import compute_betas, last_us_moves, score_universe
from notify import format_message, kst_today_str, send_telegram
from universe import US_DRIVERS

DATA_DIR = Path(__file__).parent / "data"


def _save_full_universe(full_listing: pd.DataFrame, as_of) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = DATA_DIR / "universe.csv"
    df = full_listing.copy()
    df.insert(0, "snapshot_date", as_of.strftime("%Y-%m-%d"))
    df.to_csv(snapshot_path, encoding="utf-8-sig")
    print(f"[run] saved full universe snapshot: {snapshot_path} (rows={len(df)})")


def _save_picking_universe(picking: pd.DataFrame, as_of) -> None:
    path = DATA_DIR / "picking_universe.csv"
    df = picking.copy()
    df.insert(0, "snapshot_date", as_of.strftime("%Y-%m-%d"))
    df.to_csv(path, encoding="utf-8-sig")
    print(f"[run] saved picking universe: {path} (rows={len(df)})")


def main():
    try:
        as_of = latest_trading_day_kr()
        print(f"[run] latest KR trading day: {as_of}")

        full = fetch_kr_full_listing()
        print(f"[run] FULL KR listing (KOSPI+KOSDAQ): {len(full)} stocks")
        _save_full_universe(full, as_of)

        meta = filter_to_picking_universe(full)
        _save_picking_universe(meta, as_of)
        print(f"[run] picking universe (시총 ≥ 1000억): {len(meta)} stocks")

        us_prices = fetch_prices(US_DRIVERS)
        kr_prices, kr_volume = fetch_prices(meta["yf_ticker"].tolist(), with_volume=True)
        print(
            f"[run] US prices: {us_prices.shape}, "
            f"KR prices: {kr_prices.shape}, KR volume: {kr_volume.shape}"
        )

        if us_prices.empty or kr_prices.empty:
            raise RuntimeError("Price fetch failed (empty US or KR)")

        us_ret = daily_returns(us_prices)
        kr_ret = daily_returns(kr_prices)

        meta = meta[meta["yf_ticker"].isin(kr_prices.columns)]
        print(f"[run] KR universe with prices: {len(meta)}")

        betas = compute_betas(kr_ret, us_ret)
        print(f"[run] computed betas for {len(betas)} KR stocks")

        last_us = last_us_moves(us_ret)
        print(f"[run] last US session: {len(last_us)} drivers with returns")

        yf_to_code = {row.yf_ticker: code for code, row in meta.iterrows()}
        vol_by_code = kr_volume.rename(columns=yf_to_code)
        vol_by_code = vol_by_code[[c for c in vol_by_code.columns if c in meta.index]]

        picks = score_universe(betas, last_us, meta, vol_by_code)
        print(f"[run] picks: {len(picks)}")

        msg = format_message(picks, last_us, kst_today_str())
        print("------ message ------")
        print(msg)
        print("---------------------")

        result = send_telegram(msg)
        print(f"[run] telegram ok: message_id={result['result']['message_id']}")
    except Exception as e:
        traceback.print_exc()
        try:
            send_telegram(
                f"⚠️ [{kst_today_str()}] 단타 스크리닝 실패\n\n"
                f"{type(e).__name__}: {e}\n\n로그 확인 필요"
            )
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
