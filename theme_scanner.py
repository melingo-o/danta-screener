"""Narrow theme strength scanner — detect overnight US theme moves and surface KR mappings.

Complements morning_picks (which targets broad sector ETF beta via universe.py). This
catches narrow themes like 양자/SMR/휴머노이드/비만 that don't show up in broad SMH/XLK/XLV.

Trigger (aggressive — chosen by user):
  max(us_proxy moves) ≥ +5%   AND
  mean(us_proxy moves) ≥ +2%

No scoring per KR stock — just surfaces the mapped list for the user's discretion.
This is a complement to morning_picks, not a replacement.

Standalone usage: `python theme_scanner.py` prints the block.
Imported by run.py to append into the morning Telegram message.
"""

from typing import Dict, List

import pandas as pd

from data import daily_returns, fetch_ohlcv
from themes import THEMES, all_us_proxies

TRIGGER_MAX_PCT = 5.0    # at least one proxy must move ≥ this %
TRIGGER_MEAN_PCT = 2.0   # average across all (non-NaN) proxies must be ≥ this %


def evaluate_themes() -> List[Dict]:
    """Fetch overnight US proxy moves, evaluate triggers, return list of fired themes
    sorted by mean_move desc."""
    proxies = all_us_proxies()
    us = fetch_ohlcv(proxies)
    closes = us.get("Close", pd.DataFrame())
    if closes.empty:
        return []
    rets = daily_returns(closes)
    if rets.empty:
        return []
    last = rets.iloc[-1].dropna()  # last session return, pct as decimal

    fired = []
    for key, theme in THEMES.items():
        moves_pct = {}
        for sym in theme["us_proxies"]:
            v = last.get(sym)
            if v is None or v != v:  # NaN
                continue
            moves_pct[sym] = float(v) * 100

        if not moves_pct:
            continue

        max_move = max(moves_pct.values())
        mean_move = sum(moves_pct.values()) / len(moves_pct)

        if max_move >= TRIGGER_MAX_PCT and mean_move >= TRIGGER_MEAN_PCT:
            fired.append({
                "key": key,
                "label": theme["label"],
                "us_moves_sorted": sorted(moves_pct.items(), key=lambda kv: -kv[1]),
                "max_move": max_move,
                "mean_move": mean_move,
                "kr_stocks": theme["kr_stocks"],
            })

    fired.sort(key=lambda x: -x["mean_move"])
    return fired


def format_themes_block(fired: List[Dict]) -> str:
    """Format fired themes into a multi-line Telegram block. Empty string if none."""
    if not fired:
        return ""
    lines = ["", "🚀 어젯밤 미장 테마 강세"]
    for f in fired:
        top_us = f["us_moves_sorted"][:3]
        us_str = " · ".join(f"{sym} {pct:+.1f}%" for sym, pct in top_us)
        lines.append("")
        lines.append(f"{f['label']}  (평균 {f['mean_move']:+.1f}%)")
        lines.append(f"   📈 {us_str}")
        kr_str = " · ".join(f"{name}({code})" for code, name in f["kr_stocks"][:6])
        lines.append(f"   🇰🇷 {kr_str}")
    return "\n".join(lines)


def main():
    """Standalone CLI for testing — prints the block, no Telegram send."""
    fired = evaluate_themes()
    if not fired:
        print("[theme_scanner] no themes triggered")
        return
    print(format_themes_block(fired))


if __name__ == "__main__":
    main()
