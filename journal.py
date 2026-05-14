"""Prediction journal: append every morning's picks + later record actuals.

Two CSVs in data/journal/:
  picks.csv   — append-only, one row per pick on morning
  results.csv — append-only, one row per pick AFTER actual gap & 30-min outcome known
"""

import csv
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, List

import pytz

if TYPE_CHECKING:
    from model import Pick  # noqa: F401  (type hints only)

JOURNAL_DIR = Path(__file__).parent / "data" / "journal"
PICKS_CSV = JOURNAL_DIR / "picks.csv"
RESULTS_CSV = JOURNAL_DIR / "results.csv"

PICKS_FIELDS = [
    "date", "rank", "ticker6", "name", "market_cap",
    "predicted_gap", "predicted_gap_net", "vol_ratio",
    "primary_driver", "drivers_str",
]

RESULTS_FIELDS = [
    "date", "rank", "ticker6", "name",
    "predicted_gap", "predicted_gap_net",
    "actual_gap", "actual_open_to_930",
    "actual_high_pct", "actual_low_pct",
    "hit_gross", "hit_net", "tradeable_30m",
]


def _today_kst() -> str:
    return datetime.now(pytz.timezone("Asia/Seoul")).strftime("%Y-%m-%d")


def _write_rows(path: Path, fields: List[str], rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if is_new:
            w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def append_picks(picks: "List[Pick]", date_str: str = None) -> None:
    date_str = date_str or _today_kst()
    rows = []
    for i, p in enumerate(picks, 1):
        drivers_str = "; ".join(f"{us}:β{beta:+.2f}@{contrib*100:+.2f}%" for us, beta, contrib in p.drivers)
        rows.append({
            "date": date_str,
            "rank": i,
            "ticker6": p.ticker6,
            "name": p.name,
            "market_cap": int(p.market_cap),
            "predicted_gap": round(p.expected_return, 5),
            "predicted_gap_net": round(p.expected_return_net, 5),
            "vol_ratio": round(p.volume_ratio, 3) if p.volume_ratio == p.volume_ratio else "",
            "primary_driver": p.primary_driver or "",
            "drivers_str": drivers_str,
        })
    _write_rows(PICKS_CSV, PICKS_FIELDS, rows)
    print(f"[journal] appended {len(rows)} picks for {date_str} → {PICKS_CSV}")


def append_results(rows: List[dict]) -> None:
    if not rows:
        return
    _write_rows(RESULTS_CSV, RESULTS_FIELDS, rows)
    print(f"[journal] appended {len(rows)} results → {RESULTS_CSV}")


def read_picks_for_date(date_str: str) -> List[dict]:
    """Return all picks from picks.csv matching date_str."""
    if not PICKS_CSV.exists():
        return []
    out = []
    with PICKS_CSV.open("r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("date") == date_str:
                out.append(row)
    return out


def latest_picks_date() -> str:
    """Return the most recent date string present in picks.csv, or empty string if none."""
    if not PICKS_CSV.exists():
        return ""
    dates = set()
    with PICKS_CSV.open("r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            d = row.get("date", "")
            if d:
                dates.add(d)
    return max(dates) if dates else ""


def read_all_results() -> List[dict]:
    if not RESULTS_CSV.exists():
        return []
    with RESULTS_CSV.open("r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))
