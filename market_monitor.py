"""Poll KOSPI/KOSDAQ via KIS and alert on threshold crossings (vs today's open).

Designed to run every 5 minutes during KR trading hours.
State (which thresholds already alerted today) persists in data/market_state.json.
"""

import json
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import List

import pytz

from kis import KISClient, KOSPI, KOSDAQ
from telegram_client import send_telegram

STATE_FILE = Path("data/market_state.json")

# % thresholds (vs today's open). Alert each ±threshold once per day.
THRESHOLDS = [1.0, 2.0, 3.0, 5.0]

INDICES = [
    {"code": KOSPI, "name": "KOSPI"},
    {"code": KOSDAQ, "name": "KOSDAQ"},
]


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


def kst_now():
    return datetime.now(pytz.timezone("Asia/Seoul"))


def check_crossings(already: List[float], change_pct: float, thresholds: List[float]) -> List[float]:
    """Return newly crossed thresholds (signed). Mutates `already` list."""
    crossed = []
    for t in thresholds:
        if change_pct >= t and t not in already:
            crossed.append(t)
            already.append(t)
        if change_pct <= -t and -t not in already:
            crossed.append(-t)
            already.append(-t)
    return crossed


def main():
    now = kst_now()
    today = now.strftime("%Y-%m-%d")
    state = load_state()
    if state.get("date") != today:
        state = {"date": today}

    client = KISClient()
    alerts = []
    summary_lines = []

    for idx in INDICES:
        name = idx["name"]
        try:
            out = client.get_index_price(idx["code"])
            current = float(out["bstp_nmix_prpr"])
            open_p = float(out["bstp_nmix_oprc"])
            prev_diff_pct = float(out["bstp_nmix_prdy_ctrt"])  # vs prev close
            high = float(out["bstp_nmix_hgpr"])
            low = float(out["bstp_nmix_lwpr"])
            from_open = (current - open_p) / open_p * 100 if open_p > 0 else 0.0

            summary_lines.append(
                f"{name}: {current:,.2f} (전일대비 {prev_diff_pct:+.2f}%, "
                f"시가대비 {from_open:+.2f}%, 일중 {low:,.2f}~{high:,.2f})"
            )

            key = f"{name}_crossed"
            already = state.setdefault(key, [])
            crossed = check_crossings(already, from_open, THRESHOLDS)

            for t in crossed:
                direction = "↗️ 급등" if t > 0 else "↘️ 급락"
                alerts.append(
                    f"{direction}  {name} 시가 대비 {t:+.1f}% 돌파\n"
                    f"   현재 {current:,.2f}  (전일 {prev_diff_pct:+.2f}%)"
                )
        except Exception as e:
            print(f"  [{name}] FAILED: {e}")
            summary_lines.append(f"{name}: 조회 실패 ({e})")

    print(f"[{now.strftime('%H:%M')}] " + " | ".join(summary_lines))

    if alerts:
        header = f"🔔 시장 알림  [{now.strftime('%H:%M KST')}]\n\n"
        body = "\n\n".join(alerts)
        msg = header + body
        print("--- alert ---")
        print(msg)
        print("-------------")
        send_telegram(msg)

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
