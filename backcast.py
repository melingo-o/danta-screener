"""Backcast: simulate running the bot on a past KR open date using only data BEFORE that date.

Usage:
    BACKCAST_DATE=2026-05-12 python backcast.py
"""

import os
import sys
import traceback

import pandas as pd

from data import (
    daily_returns,
    fetch_kr_full_listing,
    fetch_ohlcv,
    filter_to_picking_universe,
    gap_returns,
)
from model import compute_betas, score_universe
from notify import format_message, send_telegram
from universe import US_DRIVERS


def main():
    target_date_str = os.environ.get("BACKCAST_DATE")
    if not target_date_str:
        print("ERROR: set BACKCAST_DATE=YYYY-MM-DD")
        sys.exit(2)
    target_date = pd.Timestamp(target_date_str).normalize()
    print(f"[backcast] Simulating morning of KR date: {target_date.date()}")

    full = fetch_kr_full_listing()
    meta = filter_to_picking_universe(full)
    print(f"[backcast] Universe (시총 ≥ 1,000억): {len(meta)} stocks")

    us = fetch_ohlcv(US_DRIVERS)
    kr = fetch_ohlcv(meta["yf_ticker"].tolist())

    us_ret = daily_returns(us["Close"])
    kr_gap = gap_returns(kr["Open"], kr["Close"])

    # Slice to data BEFORE target_date — strict no-lookahead
    us_train = us_ret.loc[us_ret.index < target_date]
    kr_train = kr_gap.loc[kr_gap.index < target_date]
    vol_train = kr["Volume"].loc[kr["Volume"].index < target_date]

    if us_train.empty or kr_train.empty:
        raise RuntimeError(f"Not enough history before {target_date.date()}")

    print(
        f"[backcast] Training: US through {us_train.index[-1].date()}, "
        f"KR through {kr_train.index[-1].date()}"
    )

    kr_train = kr_train.dropna(thresh=int(len(kr_train) * 0.7), axis=1)
    meta_filt = meta[meta["yf_ticker"].isin(kr_train.columns)]
    print(f"[backcast] KR with sufficient history: {len(meta_filt)}")

    betas = compute_betas(kr_train, us_train)
    print(f"[backcast] Betas computed for {len(betas)} stocks")

    last_us = us_train.iloc[-1].dropna()
    print(f"[backcast] Last US session used: {us_train.index[-1].date()}")

    yf_to_code = {row.yf_ticker: code for code, row in meta_filt.iterrows()}
    vol_by_code = vol_train.rename(columns=yf_to_code)
    vol_by_code = vol_by_code[[c for c in vol_by_code.columns if c in meta_filt.index]]

    picks = score_universe(betas, last_us, meta_filt, vol_by_code)
    print(f"[backcast] Picks: {len(picks)}")

    # Look up actual realized gap on target_date for these picks
    actuals = {}
    if target_date in kr_gap.index:
        row = kr_gap.loc[target_date]
        for p in picks:
            v = row.get(p.yf_ticker)
            if v == v:
                actuals[p.ticker6] = float(v)
    else:
        print(f"[backcast] WARNING: no actual gap data for {target_date.date()} (non-trading day?)")

    msg_core = format_message(picks, last_us, target_date.strftime("%Y-%m-%d"))

    header = (
        f"🔍 [백캐스트 — 실제로 안 보냈던 메시지 재구성]\n"
        f"날짜: {target_date.strftime('%Y-%m-%d')} 아침 (KR 개장 전)\n"
        f"이건 봇이 그날 아침에 보냈을 메시지를 사후 재구성한 것.\n"
        f"────────────────────\n\n"
    )

    footer_lines = ["", "────────────────────", "📈 실제 결과 (사후 비교):"]
    if not actuals and picks:
        footer_lines.append("(실제 갭 데이터 없음 — KR 휴장일 또는 데이터 누락)")
    else:
        for i, p in enumerate(picks, 1):
            actual = actuals.get(p.ticker6)
            if actual is not None:
                hit = "✅" if actual > 0 else "❌"
                net_hit = "💰" if actual > 0.005 else ""
                sign_e = "+" if p.expected_return >= 0 else ""
                sign_a = "+" if actual >= 0 else ""
                footer_lines.append(
                    f"{i}. {p.name} ({p.ticker6}): "
                    f"예상 {sign_e}{p.expected_return*100:.2f}% → "
                    f"실제 {sign_a}{actual*100:.2f}% {hit}{net_hit}"
                )
            else:
                footer_lines.append(f"{i}. {p.name} ({p.ticker6}): 실제 데이터 없음")

    msg = header + msg_core + "\n" + "\n".join(footer_lines)

    print("------ message ------")
    print(msg)
    print("---------------------")

    result = send_telegram(msg)
    print(f"[backcast] telegram ok: message_id={result['result']['message_id']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
