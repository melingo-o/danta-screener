"""Backcast: simulate running the bot on a past KR open date using only data BEFORE that date,
then fetch intraday 5-min bars to show the 9:00→9:30 movement for each pick.

Usage:
    BACKCAST_DATE=2026-05-12 python backcast.py
"""

import os
import sys
import traceback
from datetime import time as dtime

import pandas as pd
import yfinance as yf

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


def fetch_intraday_window(yf_ticker: str, target_date: pd.Timestamp):
    """Fetch 5-min bars for 9:00-9:30 KST on target_date for a single ticker."""
    next_day = target_date + pd.Timedelta(days=1)
    try:
        df = yf.download(
            yf_ticker,
            start=target_date.strftime("%Y-%m-%d"),
            end=next_day.strftime("%Y-%m-%d"),
            interval="5m",
            progress=False,
            auto_adjust=False,
        )
    except Exception as e:
        print(f"  [intraday] {yf_ticker} fetch failed: {e}")
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    # Normalize tz to KST
    if df.index.tz is None:
        df.index = df.index.tz_localize("Asia/Seoul")
    else:
        df.index = df.index.tz_convert("Asia/Seoul")
    target_d = target_date.date()
    mask = (
        (df.index.normalize().date == target_d) if hasattr(df.index.normalize(), "date")
        else df.index.map(lambda x: x.date() == target_d)
    )
    # safer: filter by date and time-of-day directly
    times = df.index.time
    dates = pd.Index([t.date() for t in df.index])
    keep = (dates == target_d) & (times >= dtime(9, 0)) & (times <= dtime(9, 30))
    window = df.loc[keep]
    if window.empty:
        return None
    return {
        "open_900": float(window.iloc[0]["Open"]),
        "close_930": float(window.iloc[-1]["Close"]),
        "high": float(window["High"].max()),
        "low": float(window["Low"].min()),
        "volume": float(window["Volume"].sum()),
        "n_bars": int(len(window)),
    }


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

    # Fetch intraday 9:00-9:30 window for each pick
    intraday = {}
    print("[backcast] fetching 9:00-9:30 intraday windows...")
    for p in picks:
        info = fetch_intraday_window(p.yf_ticker, target_date)
        if info:
            intraday[p.ticker6] = info
            print(f"  {p.ticker6}: open={info['open_900']:.0f}, "
                  f"high={info['high']:.0f}, low={info['low']:.0f}, "
                  f"close={info['close_930']:.0f}, bars={info['n_bars']}")
        else:
            print(f"  {p.ticker6}: no intraday data")

    footer_lines = ["", "────────────────────", "📈 사후 비교 (갭 + 9:00~9:30 추이):"]
    if not picks:
        footer_lines.append("(픽 없음)")
    else:
        for i, p in enumerate(picks, 1):
            actual = actuals.get(p.ticker6)
            intra = intraday.get(p.ticker6)

            footer_lines.append("")
            footer_lines.append(f"{i}. {p.name} ({p.ticker6})")

            # Gap line
            if actual is not None:
                hit = "✅" if actual > 0 else "❌"
                sign_e = "+" if p.expected_return >= 0 else ""
                sign_a = "+" if actual >= 0 else ""
                footer_lines.append(
                    f"   갭(9:00): 예상 {sign_e}{p.expected_return*100:.2f}% "
                    f"→ 실제 {sign_a}{actual*100:.2f}% {hit}"
                )
            else:
                footer_lines.append("   갭: 데이터 없음")

            # 9:00-9:30 window
            if intra:
                open_p = intra["open_900"]
                to_high = (intra["high"] - open_p) / open_p
                to_low = (intra["low"] - open_p) / open_p
                to_close = (intra["close_930"] - open_p) / open_p
                footer_lines.append(
                    f"   9:00→9:30 (시가 대비): "
                    f"최고 {to_high*100:+.2f}% | 9:30 {to_close*100:+.2f}% | 최저 {to_low*100:+.2f}%"
                )
                # Tradeable window assessment (beating 0.5% cost)
                if to_high > 0.005:
                    footer_lines.append(
                        f"   🎯 단타 가능: 시가 매수 → 30분 내 고점 매도 시 +{to_high*100:.2f}%"
                    )
                elif to_close > 0.005:
                    footer_lines.append(
                        f"   ⚠️ 9:30까지 +{to_close*100:.2f}% (소폭). 손절 라인 짧게."
                    )
                else:
                    footer_lines.append(
                        f"   ❌ 9:00~9:30 구간 먹을 수익 거의 없음 (최고 +{to_high*100:.2f}%)"
                    )
            else:
                footer_lines.append("   9:00~9:30: 인트라데이 데이터 없음")

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
