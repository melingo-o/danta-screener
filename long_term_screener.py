"""Long-term value screener — quantitative 7-check fundamental scoring for KR + US.

Universe:
  KR: KOSPI/KOSDAQ market cap >= 1조 (~100-150 stocks, KOSPI100ish)
  US: S&P 500 via FinanceDataReader (~500 stocks)

Per-stock (yfinance .info):
  1. ROE ≥ 15%
  2. FCF > 0 (current)
  3. Debt/Equity ≤ 100%
  4. PER reasonable (0 < PER ≤ 30)
  5. PEG ≤ 1.5
  6. Revenue growth YoY ≥ 0%
  7. Operating margin ≥ 5%

Also surfaces (for qualitative review in dashboard detail view):
  - longBusinessSummary (사업 한 문장)
  - dividend_yield / payout_ratio (자본배분)
  - current_ratio (유동성)
  - sector / industry

Output: data/long_term_scores.json — consumed by docs/ dashboard.

KR fundamental coverage in yfinance is partial; missing fields fail-safe (count as
0 in that check). Future Phase 2.5 can add OpenDART for KR-specific accuracy.
"""

import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import FinanceDataReader as fdr
import pandas as pd
import pytz
import yfinance as yf

DATA_DIR = Path(__file__).parent / "data"
OUTPUT_PATH = DATA_DIR / "long_term_scores.json"

# Universe sizing (keep modest so weekly fetch finishes in <30 min)
KR_MIN_MARKET_CAP_KRW = 1_000_000_000_000  # 1조
US_INCLUDE_SP500 = True

# Thresholds
ROE_MIN = 0.15
DEBT_EQUITY_MAX = 100.0
PER_MAX = 30.0
PEG_MAX = 1.5
REV_GROWTH_MIN = 0.0
OP_MARGIN_MIN = 0.05

MAX_WORKERS = 6           # parallel yfinance fetch
RATE_LIMIT_SLEEP = 0.1    # per-future delay
PER_TICKER_TIMEOUT = 20   # seconds (set in yfinance call)


def kst_now_iso() -> str:
    return datetime.now(pytz.timezone("Asia/Seoul")).isoformat(timespec="seconds")


def fetch_kr_universe() -> List[Dict]:
    listing = fdr.StockListing("KRX")
    listing = listing.dropna(subset=["Code", "Marcap"])
    listing["Code"] = listing["Code"].astype(str).str.zfill(6)
    listing = listing[listing["Market"].isin(["KOSPI", "KOSDAQ"])]
    listing = listing[listing["Marcap"] >= KR_MIN_MARKET_CAP_KRW]
    listing = listing.sort_values("Marcap", ascending=False)

    out = []
    for _, row in listing.iterrows():
        suffix = "KS" if row["Market"] == "KOSPI" else "KQ"
        out.append({
            "ticker": row["Code"],
            "yf_ticker": f"{row['Code']}.{suffix}",
            "name": row["Name"],
            "market": "KR",
            "market_cap_krw": float(row["Marcap"]),
        })
    return out


def fetch_us_universe() -> List[Dict]:
    """Try FDR S&P 500 first (most reliable in CI), fallback to wikipedia scrape."""
    out = []
    try:
        sp500 = fdr.StockListing("S&P500")
        for _, row in sp500.iterrows():
            sym_raw = row.get("Symbol") or row.get("symbol") or ""
            name_raw = row.get("Name") or row.get("name") or sym_raw
            sym = str(sym_raw).replace(".", "-")  # BRK.B -> BRK-B for yfinance
            if not sym:
                continue
            out.append({
                "ticker": sym,
                "yf_ticker": sym,
                "name": str(name_raw),
                "market": "US",
                "market_cap_krw": None,
            })
        if out:
            return out
    except Exception as e:
        print(f"[us] FDR S&P 500 failed: {e}")

    # Fallback: wikipedia
    try:
        df = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]
        sym_col = "Symbol" if "Symbol" in df.columns else df.columns[0]
        name_col = "Security" if "Security" in df.columns else df.columns[1]
        for _, row in df.iterrows():
            sym = str(row[sym_col]).replace(".", "-")
            out.append({
                "ticker": sym,
                "yf_ticker": sym,
                "name": str(row[name_col]),
                "market": "US",
                "market_cap_krw": None,
            })
    except Exception as e:
        print(f"[us] wikipedia fallback failed: {e}")
    return out


def fetch_fundamentals(yf_ticker: str) -> Optional[Dict]:
    try:
        t = yf.Ticker(yf_ticker)
        info = t.info or {}
        # An empty-ish info dict (no marketCap, no name) means yf had nothing useful
        if not info.get("marketCap") and not info.get("longName") and not info.get("shortName"):
            return None
        return info
    except Exception:
        return None


def score_stock(info: Dict) -> Dict:
    roe = info.get("returnOnEquity")
    fcf = info.get("freeCashflow")
    de = info.get("debtToEquity")
    per = info.get("trailingPE")
    peg = info.get("trailingPegRatio") or info.get("pegRatio")
    rev_growth = info.get("revenueGrowth")
    op_margin = info.get("operatingMargins")

    passes = {
        "roe_15": (roe is not None and roe >= ROE_MIN),
        "fcf_positive": (fcf is not None and fcf > 0),
        "debt_safe": (de is not None and 0 <= de <= DEBT_EQUITY_MAX),
        "per_reasonable": (per is not None and 0 < per <= PER_MAX),
        "peg_attractive": (peg is not None and 0 < peg <= PEG_MAX),
        "revenue_growing": (rev_growth is not None and rev_growth >= REV_GROWTH_MIN),
        "margin_healthy": (op_margin is not None and op_margin >= OP_MARGIN_MIN),
    }
    score = sum(1 for v in passes.values() if v)

    return {
        "score": score,
        "passes": passes,
        "metrics": {
            "roe": roe,
            "fcf": fcf,
            "debt_equity": de,
            "per": per,
            "peg": peg,
            "revenue_growth": rev_growth,
            "operating_margin": op_margin,
            "market_cap": info.get("marketCap"),
            "dividend_yield": info.get("dividendYield"),
            "payout_ratio": info.get("payoutRatio"),
            "current_ratio": info.get("currentRatio"),
        },
    }


def _process_one(stock: Dict) -> Optional[Dict]:
    info = fetch_fundamentals(stock["yf_ticker"])
    if info is None:
        return None
    sc = score_stock(info)
    return {
        "ticker": stock["ticker"],
        "yf_ticker": stock["yf_ticker"],
        "name": stock["name"],
        "market": stock["market"],
        "score": sc["score"],
        "passes": sc["passes"],
        "metrics": sc["metrics"],
        "industry": info.get("industry", "") or "",
        "sector": info.get("sector", "") or "",
        "summary": (info.get("longBusinessSummary") or "")[:300],
        "website": info.get("website", "") or "",
    }


def screen_universe(universe: List[Dict], limit: Optional[int] = None) -> List[Dict]:
    targets = universe[:limit] if limit else universe
    n = len(targets)
    results: List[Dict] = []
    completed = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(_process_one, s): s for s in targets}
        for fut in as_completed(futures):
            try:
                r = fut.result(timeout=PER_TICKER_TIMEOUT)
                if r is not None:
                    results.append(r)
                else:
                    failed += 1
            except Exception:
                failed += 1
            completed += 1
            if completed % 25 == 0:
                print(f"  [{completed}/{n}] done (ok={len(results)}, fail={failed})")
            time.sleep(RATE_LIMIT_SLEEP)
    print(f"  total: {completed}/{n} (ok={len(results)}, fail={failed})")
    return results


def main():
    started_at = kst_now_iso()
    print(f"[screener] start: {started_at}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    kr = fetch_kr_universe()
    print(f"[screener] KR universe: {len(kr)} stocks (cap >= {KR_MIN_MARKET_CAP_KRW/1e8:,.0f}억)")

    us = fetch_us_universe() if US_INCLUDE_SP500 else []
    print(f"[screener] US universe: {len(us)} stocks")

    limit = int(os.environ.get("SCREENER_LIMIT", 0)) or None
    if limit:
        print(f"[screener] LIMIT={limit} (debug mode)")

    print("\n[screener] scoring KR ...")
    kr_results = screen_universe(kr, limit=limit)
    print(f"[screener] KR: {len(kr_results)} scored")

    print("\n[screener] scoring US ...")
    us_results = screen_universe(us, limit=limit)
    print(f"[screener] US: {len(us_results)} scored")

    all_results = kr_results + us_results

    output = {
        "updated_at": kst_now_iso(),
        "started_at": started_at,
        "kr_universe_size": len(kr),
        "us_universe_size": len(us),
        "kr_scored": len(kr_results),
        "us_scored": len(us_results),
        "thresholds": {
            "roe_min": ROE_MIN,
            "debt_equity_max": DEBT_EQUITY_MAX,
            "per_max": PER_MAX,
            "peg_max": PEG_MAX,
            "revenue_growth_min": REV_GROWTH_MIN,
            "operating_margin_min": OP_MARGIN_MIN,
        },
        "checklist_order": [
            "roe_15", "fcf_positive", "debt_safe",
            "per_reasonable", "peg_attractive",
            "revenue_growing", "margin_healthy",
        ],
        "checklist_labels_kr": {
            "roe_15": "ROE ≥ 15%",
            "fcf_positive": "FCF > 0",
            "debt_safe": "부채비율 ≤ 100%",
            "per_reasonable": "PER 합리적 (0~30)",
            "peg_attractive": "PEG ≤ 1.5",
            "revenue_growing": "매출 성장 (YoY)",
            "margin_healthy": "영업이익률 ≥ 5%",
        },
        "stocks": all_results,
    }

    OUTPUT_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[screener] wrote {OUTPUT_PATH}")

    # Top-10 summary
    top10 = sorted(all_results, key=lambda s: -s["score"])[:10]
    print(f"\nTop 10 by score:")
    for s in top10:
        cap_b = (s["metrics"].get("market_cap") or 0) / 1e9
        print(f"  {s['score']}/7  {s['market']}  {s['ticker']:8} {s['name'][:28]:28} cap={cap_b:6.1f}B")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
