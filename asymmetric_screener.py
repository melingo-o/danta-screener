"""Asymmetric Bet Screener — score venture-style small-cap candidates.

Different from long_term_screener.py (which uses Buffett quality criteria —
mature companies at fair price). This one looks for the opposite: small-cap
companies in secular-trend themes where a 10-100x outcome is mathematically
possible if the thesis plays out.

Score components (0-100):
  - Cap sweet spot (sweet at $2-15B, penalty for too small or too large)
  - Revenue growth (J-curve: ≥100% gold, ≥25% good)
  - Margin trajectory (positive is great; negative-but-improving is OK)
  - FCF positive (rare in this stage — big bonus)
  - Recent revenue acceleration (this quarter vs last 4 quarters)

Output: data/asymmetric_scores.json — consumed by docs/ dashboard Game B tab.

This is venture investing in public markets. Most names will draw down 50-80%
at some point; 1-2 out of 10 might 10-50x over 5 years. Diversification is not
optional — it's the only thing that makes the math work.
"""

import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pytz
import yfinance as yf

from asymmetric_universe import ASYMMETRIC_THEMES, all_tickers

DATA_DIR = Path(__file__).parent / "data"
OUTPUT_PATH = DATA_DIR / "asymmetric_scores.json"

MAX_WORKERS = 6
PER_TICKER_TIMEOUT = 25


def kst_now_iso() -> str:
    return datetime.now(pytz.timezone("Asia/Seoul")).isoformat(timespec="seconds")


def fetch_full(yf_ticker: str) -> Optional[Dict]:
    """Fetch info + quarterly financials. Returns combined dict or None."""
    try:
        t = yf.Ticker(yf_ticker)
        info = t.info or {}
        if not info.get("marketCap") and not info.get("longName") and not info.get("shortName"):
            return None

        # Quarterly revenue for acceleration detection
        quarterly_rev = []
        try:
            qf = t.quarterly_financials
            if qf is not None and not qf.empty:
                rev_row = None
                for candidate in ("Total Revenue", "Revenue", "TotalRevenue"):
                    if candidate in qf.index:
                        rev_row = qf.loc[candidate]
                        break
                if rev_row is not None:
                    quarterly_rev = [float(v) for v in rev_row.tolist() if v is not None and not pd.isna(v)]
        except Exception:
            pass

        return {"info": info, "quarterly_rev": quarterly_rev}
    except Exception:
        return None


def compute_acceleration(quarterly_rev: List[float]) -> Optional[float]:
    """Return fractional acceleration: (latest QoQ growth) - (avg QoQ over prior 3).
    Positive = accelerating. yfinance returns most-recent first.
    """
    if not quarterly_rev or len(quarterly_rev) < 4:
        return None
    # Most recent first → reverse to chronological
    qs = list(reversed(quarterly_rev[:5]))
    growths = []
    for i in range(1, len(qs)):
        if qs[i - 1] <= 0:
            continue
        growths.append((qs[i] - qs[i - 1]) / qs[i - 1])
    if len(growths) < 2:
        return None
    latest = growths[-1]
    prior_avg = sum(growths[:-1]) / len(growths[:-1])
    return latest - prior_avg


def score_asymmetric(payload: Dict) -> Dict:
    """Compute asymmetric bet score and break it down for transparency."""
    info = payload.get("info") or {}
    qrev = payload.get("quarterly_rev") or []

    cap = info.get("marketCap")
    rev_growth = info.get("revenueGrowth")          # YoY decimal
    op_margin = info.get("operatingMargins")
    fcf = info.get("freeCashflow")
    accel = compute_acceleration(qrev)

    # ---- Cap score: sweet spot $2-15B ----
    cap_score = 0
    if cap is None:
        cap_score = 5  # unknown — small penalty
    elif cap < 200_000_000:
        cap_score = 5      # too small (pre-revenue often)
    elif cap < 1_000_000_000:
        cap_score = 20     # micro cap — high asymmetry but high risk
    elif cap < 5_000_000_000:
        cap_score = 35     # $1-5B sweet spot lower
    elif cap < 15_000_000_000:
        cap_score = 35     # $5-15B sweet spot upper
    elif cap < 30_000_000_000:
        cap_score = 20     # $15-30B — still possible but smaller multiple
    elif cap < 100_000_000_000:
        cap_score = 8      # too large for 10x+ probably
    else:
        cap_score = 0      # mega cap

    # ---- Revenue growth (J-curve) ----
    rev_score = 0
    if rev_growth is None:
        rev_score = 0
    elif rev_growth >= 1.0:
        rev_score = 30
    elif rev_growth >= 0.5:
        rev_score = 22
    elif rev_growth >= 0.25:
        rev_score = 15
    elif rev_growth >= 0.10:
        rev_score = 8
    elif rev_growth >= 0:
        rev_score = 3
    else:
        rev_score = 0      # declining revenue is a serious flag in this category

    # ---- Acceleration ----
    accel_score = 0
    if accel is None:
        accel_score = 0
    elif accel >= 0.10:
        accel_score = 15
    elif accel >= 0.03:
        accel_score = 10
    elif accel >= 0:
        accel_score = 5

    # ---- Margin trajectory ----
    margin_score = 0
    if op_margin is None:
        margin_score = 0
    elif op_margin >= 0.20:
        margin_score = 15
    elif op_margin >= 0.05:
        margin_score = 10
    elif op_margin >= 0:
        margin_score = 6
    elif op_margin >= -0.30:
        margin_score = 3  # losing but not bleeding catastrophically
    else:
        margin_score = 0  # > 30% loss margin is a red flag

    # ---- FCF positive bonus ----
    fcf_score = 0
    if fcf is not None and fcf > 0:
        fcf_score = 10

    total = cap_score + rev_score + accel_score + margin_score + fcf_score
    total = max(0, min(100, total))

    return {
        "score": total,
        "breakdown": {
            "cap": cap_score,
            "revenue_growth": rev_score,
            "acceleration": accel_score,
            "margin": margin_score,
            "fcf": fcf_score,
        },
        "metrics": {
            "market_cap": cap,
            "revenue_growth": rev_growth,
            "revenue_acceleration": accel,
            "operating_margin": op_margin,
            "fcf": fcf,
            "trailing_pe": info.get("trailingPE"),
            "employees": info.get("fullTimeEmployees"),
        },
        "info_extras": {
            "sector": info.get("sector", "") or "",
            "industry": info.get("industry", "") or "",
            "summary": (info.get("longBusinessSummary") or "")[:400],
            "website": info.get("website", "") or "",
        },
    }


def _process_one(theme_key: str, theme_label: str, theme_thesis: str,
                 ticker: str, name: str) -> Optional[Dict]:
    payload = fetch_full(ticker)
    if payload is None:
        return None
    sc = score_asymmetric(payload)
    return {
        "theme": theme_key,
        "theme_label": theme_label,
        "theme_thesis": theme_thesis,
        "ticker": ticker,
        "name": name,
        "score": sc["score"],
        "breakdown": sc["breakdown"],
        "metrics": sc["metrics"],
        "sector": sc["info_extras"]["sector"],
        "industry": sc["info_extras"]["industry"],
        "summary": sc["info_extras"]["summary"],
        "website": sc["info_extras"]["website"],
    }


def screen_all() -> List[Dict]:
    tasks = []
    for theme_key, theme in ASYMMETRIC_THEMES.items():
        for ticker, name in theme["tickers"]:
            tasks.append((theme_key, theme["label"], theme["thesis"], ticker, name))

    results: List[Dict] = []
    completed = 0
    n = len(tasks)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(_process_one, *t): t for t in tasks}
        for fut in as_completed(futures):
            try:
                r = fut.result(timeout=PER_TICKER_TIMEOUT)
                if r is not None:
                    results.append(r)
            except Exception as e:
                t = futures[fut]
                print(f"  [{t[3]}] failed: {e}")
            completed += 1
            if completed % 10 == 0:
                print(f"  [{completed}/{n}] processed")
    return results


def main():
    started = kst_now_iso()
    print(f"[asymmetric] start: {started}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[asymmetric] universe: {len(all_tickers())} unique tickers across {len(ASYMMETRIC_THEMES)} themes")

    results = screen_all()
    print(f"[asymmetric] scored: {len(results)}")

    # Per-theme rollup
    by_theme: Dict[str, List[Dict]] = {}
    for r in results:
        by_theme.setdefault(r["theme"], []).append(r)
    for k in by_theme:
        by_theme[k].sort(key=lambda s: -s["score"])

    output = {
        "updated_at": kst_now_iso(),
        "started_at": started,
        "n_themes": len(ASYMMETRIC_THEMES),
        "n_tickers": len(results),
        "theme_order": list(ASYMMETRIC_THEMES.keys()),
        "themes": {
            k: {
                "label": ASYMMETRIC_THEMES[k]["label"],
                "thesis": ASYMMETRIC_THEMES[k]["thesis"],
                "tickers": by_theme.get(k, []),
            } for k in ASYMMETRIC_THEMES
        },
        "score_breakdown_labels": {
            "cap": "시총 sweet spot ($2-15B 최고)",
            "revenue_growth": "매출 성장률 YoY",
            "acceleration": "매출 가속 (분기별 변곡)",
            "margin": "영업이익률 / 개선 추세",
            "fcf": "FCF (+) 보너스",
        },
        "disclaimer_kr": (
            "Asymmetric bet은 venture 투자. 10개 중 8-9개가 -50%+로 가거나 사라질 수 있음. "
            "1-2개 winner가 10-100x로 전체를 견인하는 게임. "
            "단일 종목 베팅 금지. 동일가중 10개 이상으로 분산. 5년 commitment 필요."
        ),
    }

    OUTPUT_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[asymmetric] wrote {OUTPUT_PATH}")

    # Top picks across all themes
    flat = sorted(results, key=lambda s: -s["score"])[:15]
    print(f"\nTop 15 by asymmetric score:")
    for r in flat:
        cap_b = (r["metrics"].get("market_cap") or 0) / 1e9
        rg = r["metrics"].get("revenue_growth")
        rg_s = f"{rg*100:+.0f}%" if rg is not None else "—"
        print(f"  {r['score']:3d}/100  [{r['theme']:22}] {r['ticker']:6} {r['name'][:24]:24} cap={cap_b:6.1f}B rev{rg_s}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
