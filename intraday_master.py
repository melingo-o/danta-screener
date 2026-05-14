"""Master intraday dispatcher.

Runs market_monitor + intraday_scanner + dart_monitor sequentially each tick.
Each child script handles its own time-of-day gating and idempotency.

Triggered externally (cron-job.org) every 5 min via repository_dispatch.
"""

import importlib
import sys
import traceback
from datetime import datetime

import pytz

TASKS = [
    ("market_monitor", "📊 KOSPI/KOSDAQ regime"),
    ("intraday_scanner", "📡 stock scanner"),
    ("dart_monitor", "📋 DART"),
    ("morning_recap", "📈 morning recap"),
]


def kst_now():
    return datetime.now(pytz.timezone("Asia/Seoul"))


def main():
    now = kst_now()
    hhmm = now.strftime("%H:%M")
    weekday = now.weekday()  # 0=Mon, 6=Sun

    # Only run on weekdays during/around market hours
    if weekday >= 5:
        print(f"[master] {hhmm} weekend — skip")
        return
    if not (8 <= now.hour < 18):
        print(f"[master] {hhmm} outside 08-18 KST — skip")
        return

    print(f"[master] {now.isoformat()}  weekday={weekday}")

    failures = []
    for mod_name, label in TASKS:
        # Time-gating per task
        if mod_name in ("market_monitor", "intraday_scanner"):
            if not (9 <= now.hour < 16):
                print(f"[master] skipping {label} (outside 09-16)")
                continue
        if mod_name == "morning_recap":
            # Run only in 09:35-09:55 window; idempotency guard inside script ensures one-shot
            if not (now.hour == 9 and 35 <= now.minute < 55):
                print(f"[master] skipping {label} (only 09:35-09:55)")
                continue
        print(f"\n[master] >>> {label}")
        try:
            mod = importlib.import_module(mod_name)
            if hasattr(mod, "main"):
                mod.main()
        except SystemExit as e:
            # child called sys.exit; capture and continue
            if e.code not in (None, 0):
                failures.append(f"{label} exit={e.code}")
                print(f"[master] {label} exited with code {e.code}")
        except Exception as e:
            failures.append(f"{label}: {type(e).__name__}: {e}")
            print(f"[master] {label} raised: {e}")
            traceback.print_exc()

    if failures:
        print(f"\n[master] failures: {failures}")
        sys.exit(1)
    print("\n[master] all tasks done")


if __name__ == "__main__":
    main()
