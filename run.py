"""Main entry point: fetch data → compute → send Telegram.

Model: regress KR overnight gap (Open[t] / Close[t-1] - 1) on US prev-day close return.
This targets the price move most relevant for 9:00 AM trading.
"""

import sys
import traceback
from pathlib import Path

import pandas as pd

from data import (
    daily_returns,
    fetch_kr_full_listing,
    fetch_ohlcv,
    filter_to_picking_universe,
    gap_returns,
    latest_trading_day_kr,
)
from model import compute_betas, last_us_moves, score_universe
from notify import format_message, kst_today_str, send_telegram
from universe import US_DRIVERS

DATA_DIR = Path(__file__).parent / "data"


def _save_csv(df: pd.DataFrame, name: str, as_of) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / name
    out = df.copy()
    out.insert(0, "snapshot_date", as_of.strftime("%Y-%m-%d"))
    out.to_csv(path, encoding="utf-8-sig")
    print(f"[run] saved {path} (rows={len(out)})")


def main():
    try:
        as_of = latest_trading_day_kr()
        print(f"[run] latest KR trading day: {as_of}")

        full = fetch_kr_full_listing()
        print(f"[run] FULL KR listing (KOSPI+KOSDAQ): {len(full)} stocks")
        _save_csv(full, "universe.csv", as_of)

        meta = filter_to_picking_universe(full)
        _save_csv(meta, "picking_universe.csv", as_of)
        print(f"[run] picking universe (시총 ≥ 1000억): {len(meta)} stocks")

        us = fetch_ohlcv(US_DRIVERS)
        kr = fetch_ohlcv(meta["yf_ticker"].tolist())
        print(
            f"[run] US Close: {us['Close'].shape}, "
            f"KR Open: {kr['Open'].shape}, KR Close: {kr['Close'].shape}, "
            f"KR Volume: {kr['Volume'].shape}"
        )

        if us["Close"].empty or kr["Open"].empty or kr["Close"].empty:
            raise RuntimeError("Price fetch failed (empty OHLCV)")

        us_ret = daily_returns(us["Close"])
        kr_gap = gap_returns(kr["Open"], kr["Close"])
        print(f"[run] US returns: {us_ret.shape}, KR gap returns: {kr_gap.shape}")

        meta = meta[meta["yf_ticker"].isin(kr["Close"].columns)]
        print(f"[run] KR universe with prices: {len(meta)}")

        betas = compute_betas(kr_gap, us_ret)
        print(f"[run] computed gap betas for {len(betas)} KR stocks")

        last_us = last_us_moves(us_ret)
        print(f"[run] last US session: {len(last_us)} drivers with returns")

        yf_to_code = {row.yf_ticker: code for code, row in meta.iterrows()}
        vol_by_code = kr["Volume"].rename(columns=yf_to_code)
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
