"""Morning recap (9:40 KST): fetch actual 9:00 open and 9:00-9:30 movement for
today's picks. Compare to predictions, append to results journal, send summary
to Telegram with rolling stats.
"""

import sys
import traceback
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pytz

from journal import (
    append_results,
    latest_picks_date,
    read_all_results,
    read_picks_for_date,
)
from kis import KISClient
from telegram_client import send_telegram


def kst_now():
    return datetime.now(pytz.timezone("Asia/Seoul"))


def parse_bars(bars: List[dict]) -> List[dict]:
    """Filter to 9:00-9:30 bars, return list of {hhmm, open, high, low, close, vol} sorted ascending."""
    out = []
    for b in bars:
        try:
            t = b.get("stck_cntg_hour", "")
            if not t or len(t) < 4:
                continue
            hhmm = int(t[:4])
            if hhmm < 900 or hhmm > 930:
                continue
            out.append({
                "hhmm": hhmm,
                "open": float(b["stck_oprc"]),
                "high": float(b["stck_hgpr"]),
                "low": float(b["stck_lwpr"]),
                "close": float(b["stck_prpr"]),
                "vol": float(b.get("cntg_vol", 0) or 0),
            })
        except Exception:
            continue
    out.sort(key=lambda x: x["hhmm"])
    return out


def fetch_pick_outcome(client: KISClient, ticker6: str) -> Optional[Dict]:
    """Return dict with actual_open, actual_close_930, window_high, window_low.
    Computed from KIS intraday bars 9:00-9:30."""
    try:
        bars = client.get_intraday_bars(ticker6, end_hhmmss="093000")
    except Exception as e:
        print(f"  [{ticker6}] intraday fetch failed: {e}")
        return None
    window = parse_bars(bars)
    if not window:
        return None

    # Get yesterday's close from daily bars
    try:
        daily = client.get_daily_bars(ticker6, period="D")
        # KIS returns newest first
        if len(daily) < 2:
            return None
        prev_close = float(daily[1]["stck_clpr"])
    except Exception as e:
        print(f"  [{ticker6}] daily fetch failed: {e}")
        return None

    open_9 = window[0]["open"]
    close_930 = window[-1]["close"]
    win_high = max(b["high"] for b in window)
    win_low = min(b["low"] for b in window)

    return {
        "prev_close": prev_close,
        "open_900": open_9,
        "close_930": close_930,
        "window_high": win_high,
        "window_low": win_low,
    }


def fmt_pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def rolling_stats(results: List[dict], n_days: int = 20) -> Dict:
    """Compute rolling hit rates etc. from latest N days of results."""
    if not results:
        return {}
    # Sort by date desc, take last N days
    by_date = {}
    for r in results:
        d = r.get("date", "")
        if not d:
            continue
        by_date.setdefault(d, []).append(r)
    recent_dates = sorted(by_date.keys(), reverse=True)[:n_days]
    rows = []
    for d in recent_dates:
        rows.extend(by_date[d])
    if not rows:
        return {}

    n = len(rows)
    hits = sum(1 for r in rows if r.get("hit_gross") in ("True", "1", True))
    hits_net = sum(1 for r in rows if r.get("hit_net") in ("True", "1", True))
    tradeable = sum(1 for r in rows if r.get("tradeable_30m") in ("True", "1", True))

    avg_pred = avg_actual = avg_open_to_930 = 0.0
    cnt = 0
    for r in rows:
        try:
            p = float(r.get("predicted_gap_net", 0))
            a = float(r.get("actual_gap", 0))
            a930 = float(r.get("actual_open_to_930", 0))
            avg_pred += p
            avg_actual += a
            avg_open_to_930 += a930
            cnt += 1
        except Exception:
            continue
    if cnt:
        avg_pred /= cnt
        avg_actual /= cnt
        avg_open_to_930 /= cnt

    return {
        "n_picks": n,
        "n_days": len(recent_dates),
        "hit_rate": hits / n,
        "hit_rate_net": hits_net / n,
        "tradeable_rate": tradeable / n,
        "avg_predicted_net": avg_pred,
        "avg_actual_gap": avg_actual,
        "avg_open_to_930": avg_open_to_930,
    }


def main():
    target = latest_picks_date()
    if not target:
        print("[recap] no picks journal yet — nothing to recap")
        return
    today = kst_now().strftime("%Y-%m-%d")
    if target != today:
        print(f"[recap] latest pick date is {target}, not today ({today}). Likely weekend/holiday.")

    # Idempotency: skip if already recapped today
    existing = read_all_results()
    if any(r.get("date") == target for r in existing):
        print(f"[recap] {target} already recapped — skipping")
        return

    picks_rows = read_picks_for_date(target)
    if not picks_rows:
        print(f"[recap] no picks rows for {target}")
        return
    print(f"[recap] {len(picks_rows)} picks for {target}")

    client = KISClient()
    result_rows = []
    msg_lines = [f"📈 [{target} 결산 — 9:00~9:30]\n"]

    for row in picks_rows:
        ticker6 = row["ticker6"]
        name = row["name"]
        try:
            rank = int(row.get("rank", 0))
        except Exception:
            rank = 0
        try:
            pred_gross = float(row.get("predicted_gap", 0))
            pred_net = float(row.get("predicted_gap_net", 0))
        except Exception:
            pred_gross = pred_net = 0.0

        outcome = fetch_pick_outcome(client, ticker6)
        if not outcome:
            msg_lines.append(f"{rank}. {name} ({ticker6}) — 인트라데이 데이터 없음")
            continue

        actual_gap = (outcome["open_900"] - outcome["prev_close"]) / outcome["prev_close"]
        open_to_930 = (outcome["close_930"] - outcome["open_900"]) / outcome["open_900"]
        high_pct = (outcome["window_high"] - outcome["open_900"]) / outcome["open_900"]
        low_pct = (outcome["window_low"] - outcome["open_900"]) / outcome["open_900"]
        hit_gross = actual_gap > 0
        hit_net = actual_gap > 0.005
        tradeable = high_pct > 0.005  # 시가 매수 후 30분 내 +0.5% 이상 가능

        hit_mark = "✅" if hit_gross else "❌"
        tradeable_mark = "🎯" if tradeable else "💀"

        msg_lines.append(
            f"{rank}. {name} ({ticker6}) {hit_mark}\n"
            f"   갭: 예상 {fmt_pct(pred_gross)} → 실제 {fmt_pct(actual_gap)}\n"
            f"   9:00→9:30: 최고 {fmt_pct(high_pct)} | 9:30 {fmt_pct(open_to_930)} | 최저 {fmt_pct(low_pct)}  {tradeable_mark}"
        )

        result_rows.append({
            "date": target,
            "rank": rank,
            "ticker6": ticker6,
            "name": name,
            "predicted_gap": pred_gross,
            "predicted_gap_net": pred_net,
            "actual_gap": round(actual_gap, 5),
            "actual_open_to_930": round(open_to_930, 5),
            "actual_high_pct": round(high_pct, 5),
            "actual_low_pct": round(low_pct, 5),
            "hit_gross": hit_gross,
            "hit_net": hit_net,
            "tradeable_30m": tradeable,
        })

    append_results(result_rows)

    # Rolling stats (last 20 days from results)
    stats = rolling_stats(read_all_results(), n_days=20)
    if stats:
        msg_lines.append("\n📊 최근 20일 누적:")
        msg_lines.append(
            f"  픽 {stats['n_picks']}개 / {stats['n_days']}일 | "
            f"적중률 {stats['hit_rate']*100:.0f}% (비용차감 {stats['hit_rate_net']*100:.0f}%) | "
            f"단타가능률 {stats['tradeable_rate']*100:.0f}%"
        )
        msg_lines.append(
            f"  평균 예상 {fmt_pct(stats['avg_predicted_net'])} → "
            f"실제갭 {fmt_pct(stats['avg_actual_gap'])} → "
            f"9:30 {fmt_pct(stats['avg_open_to_930'])}"
        )

    msg = "\n".join(msg_lines)
    print("--- recap ---")
    print(msg)
    print("-------------")
    send_telegram(msg)
    print("[recap] sent")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
