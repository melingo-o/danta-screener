"""Intraday scanner — every 10 min during market hours.

For a focused universe (시총 ≥ 3,000억 ~ 600 stocks): fetch current snapshot via KIS
and detect signals:
  - MA breakout: previous close < SMA20 < current price (today bullish cross of 20-day SMA)
  - RSI oversold exit: yesterday's RSI < 35 and today's close > yesterday close × 1.01
  - Volume + price surge: current volume ratio vs 20-day median > 2.5× AND price > open by ≥ 3%
  - 20-day high breakout: current price > 20-day high

Uses pre-computed daily indicators saved as data/daily_indicators.csv (built by morning_picks),
so this scanner stays light (no historical fetch per stock).

State (already-alerted today) in data/scanner_state.json.
"""

import csv
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pytz

from kis import KISClient
from telegram_client import send_telegram

INDICATORS_CSV = Path("data/daily_indicators.csv")
STATE_FILE = Path("data/scanner_state.json")

# Tunable
VOL_SURGE_RATIO = 2.5      # today vol vs 20-day median
PRICE_SURGE_PCT = 3.0      # % from today's open
MIN_MARKET_CAP_KRW = 300_000_000_000  # 3,000억
MAX_ALERTS_PER_RUN = 10    # avoid spamming


def kst_now():
    return datetime.now(pytz.timezone("Asia/Seoul"))


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def load_indicators() -> Dict[str, dict]:
    """Return dict keyed by ticker6 → indicators dict (sma20, rsi14, vol_median20, prev_close, etc.)."""
    if not INDICATORS_CSV.exists():
        return {}
    out = {}
    with INDICATORS_CSV.open("r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            t = row.get("ticker6", "")
            if not t:
                continue
            try:
                out[t] = {
                    "name": row.get("name", ""),
                    "market_cap": float(row.get("market_cap", 0) or 0),
                    "sma20": float(row.get("sma20", 0) or 0),
                    "rsi14": float(row.get("rsi14", 0) or 0),
                    "vol_median20": float(row.get("vol_median20", 0) or 0),
                    "prev_close": float(row.get("prev_close", 0) or 0),
                    "high20": float(row.get("high20", 0) or 0),
                }
            except Exception:
                continue
    return out


def detect_signals(ind: dict, snap: dict) -> List[str]:
    """Compare today's snapshot to indicators. Return list of triggered signal labels."""
    triggers = []
    cur = snap["current"]
    open_p = snap["open"]
    vol = snap["volume"]

    # MA breakout: cross above SMA20 today
    if ind["sma20"] > 0 and ind["prev_close"] > 0:
        if ind["prev_close"] < ind["sma20"] <= cur:
            triggers.append("20일선↑돌파")

    # RSI oversold exit
    if 0 < ind["rsi14"] < 35 and cur > ind["prev_close"] * 1.01:
        triggers.append(f"RSI탈출({ind['rsi14']:.0f})")

    # Volume + price surge
    if ind["vol_median20"] > 0:
        vol_ratio = vol / ind["vol_median20"]
        if open_p > 0:
            price_chg = (cur - open_p) / open_p * 100
            if vol_ratio >= VOL_SURGE_RATIO and price_chg >= PRICE_SURGE_PCT:
                triggers.append(f"거래량×{vol_ratio:.1f}+{price_chg:+.1f}%")

    # 20-day high breakout
    if ind["high20"] > 0 and cur > ind["high20"]:
        triggers.append("20일고가↑돌파")

    return triggers


def fetch_snapshot(client: KISClient, ticker6: str) -> Optional[dict]:
    try:
        out = client.get_stock_price(ticker6)
    except Exception as e:
        print(f"  [{ticker6}] price fetch failed: {e}")
        return None
    try:
        return {
            "current": float(out["stck_prpr"]),
            "open": float(out["stck_oprc"]),
            "high": float(out["stck_hgpr"]),
            "low": float(out["stck_lwpr"]),
            "volume": float(out.get("acml_vol", 0) or 0),
            "prdy_diff_pct": float(out.get("prdy_ctrt", 0) or 0),
        }
    except Exception as e:
        print(f"  [{ticker6}] parse failed: {e}")
        return None


def fmt_price(p: float) -> str:
    return f"{int(p):,}원" if p >= 1000 else f"{p:.0f}원"


def main():
    indicators = load_indicators()
    if not indicators:
        print("[scanner] no daily indicators yet — skipping (run morning_picks first)")
        return

    # Filter to candidates by market cap
    candidates = {t: ind for t, ind in indicators.items() if ind["market_cap"] >= MIN_MARKET_CAP_KRW}
    print(f"[scanner] universe: {len(candidates)} stocks (시총 ≥ {MIN_MARKET_CAP_KRW/1e8:,.0f}억)")

    state = load_state()
    today = kst_now().strftime("%Y-%m-%d")
    if state.get("date") != today:
        state = {"date": today}
    alerted = set(state.get("alerted", []))

    client = KISClient()
    alerts = []

    for ticker6, ind in candidates.items():
        # Skip if already alerted today
        if ticker6 in alerted:
            continue
        snap = fetch_snapshot(client, ticker6)
        if not snap:
            continue
        triggers = detect_signals(ind, snap)
        if not triggers:
            continue
        alerts.append({
            "ticker6": ticker6,
            "name": ind["name"],
            "snap": snap,
            "triggers": triggers,
            "market_cap": ind["market_cap"],
        })
        alerted.add(ticker6)
        if len(alerts) >= MAX_ALERTS_PER_RUN:
            break
        # KIS rate limit ~20 req/sec; small sleep is safe
        time.sleep(0.05)

    state["alerted"] = sorted(alerted)
    save_state(state)

    if not alerts:
        print("[scanner] no new signals")
        return

    # Sort alerts by # triggers desc, then by intraday %
    def sort_key(a):
        return (-len(a["triggers"]), -a["snap"]["prdy_diff_pct"])
    alerts.sort(key=sort_key)

    now_str = kst_now().strftime("%H:%M")
    lines = [f"📡 장중 스캐너 [{now_str} KST] — {len(alerts)}개 신호\n"]
    for a in alerts:
        cap = a["market_cap"]
        cap_s = f"{cap/1e12:.1f}조" if cap >= 1e12 else f"{cap/1e8:,.0f}억"
        snap = a["snap"]
        open_p = snap["open"]
        cur = snap["current"]
        from_open = (cur - open_p) / open_p * 100 if open_p > 0 else 0.0
        lines.append(
            f"• {a['name']} ({a['ticker6']}) {fmt_price(cur)} "
            f"(전일 {snap['prdy_diff_pct']:+.2f}%, 시가대비 {from_open:+.2f}%, 시총 {cap_s})\n"
            f"   ➡️ {' / '.join(a['triggers'])}"
        )

    msg = "\n".join(lines)
    print("--- scanner alert ---")
    print(msg)
    print("---------------------")
    send_telegram(msg)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
