"""Main entry point: fetch data → compute → send Telegram."""

import sys
import traceback

from data import (
    daily_returns,
    fetch_kr_recent_volume,
    fetch_kr_universe,
    fetch_us_and_kr_prices,
    latest_trading_day_kr,
)
from model import compute_betas, last_us_moves, score_universe
from notify import format_message, kst_today_str, send_telegram


def main():
    try:
        as_of = latest_trading_day_kr()
        print(f"[run] latest KR trading day: {as_of}")

        meta = fetch_kr_universe(as_of)
        print(f"[run] KR universe size: {len(meta)}")

        us_prices, kr_prices = fetch_us_and_kr_prices(meta["yf_ticker"].tolist())
        print(f"[run] US prices: {us_prices.shape}, KR prices: {kr_prices.shape}")

        if us_prices.empty or kr_prices.empty:
            raise RuntimeError("Price fetch failed (empty US or KR)")

        us_ret = daily_returns(us_prices)
        kr_ret = daily_returns(kr_prices)

        # keep only KR yf tickers we have prices for
        meta = meta[meta["yf_ticker"].isin(kr_prices.columns)]
        print(f"[run] KR universe with prices: {len(meta)}")

        betas = compute_betas(kr_ret, us_ret)
        print(f"[run] computed betas for {len(betas)} KR stocks")

        last_us = last_us_moves(us_ret)
        print(f"[run] last US session: {len(last_us)} tickers")

        recent_volume = fetch_kr_recent_volume(meta.index.tolist(), as_of)
        print(f"[run] recent volume panel: {recent_volume.shape}")

        picks = score_universe(betas, last_us, meta, recent_volume)
        print(f"[run] picks: {len(picks)}")

        msg = format_message(picks, last_us, kst_today_str())
        print("------ message ------")
        print(msg)
        print("---------------------")

        result = send_telegram(msg)
        print(f"[run] telegram ok: message_id={result['result']['message_id']}")
    except Exception as e:
        traceback.print_exc()
        # Best effort: try sending a failure notice
        try:
            send_telegram(
                f"⚠️ [{kst_today_str()}] 단타 스크리닝 실패\n\n{type(e).__name__}: {e}\n\n로그 확인 필요"
            )
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
