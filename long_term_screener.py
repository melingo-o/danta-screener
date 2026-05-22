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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import FinanceDataReader as fdr
import pandas as pd
import pytz
import yfinance as yf

DATA_DIR = Path(__file__).parent / "data"
OUTPUT_PATH = DATA_DIR / "long_term_scores.json"

# Universe sizing (keep modest so weekly fetch finishes in <30 min)
KR_MIN_MARKET_CAP_KRW = 1_000_000_000_000  # 1조
US_INCLUDE_SP500 = True

# Standard thresholds (7-check tier — "passes" filter)
ROE_MIN = 0.15
DEBT_EQUITY_MAX = 100.0
PER_MAX = 30.0
PEG_MAX = 1.5
REV_GROWTH_MIN = 0.0
OP_MARGIN_MIN = 0.05

# Must-buy ("S-tier") — Buffett-style "wonderful company at fair price".
# Stricter on every axis; meant to surface only ~5-20 names across KR+US.
MB_ROE = 0.20
MB_DE = 60.0
MB_PER = 25.0
MB_PEG = 1.0
MB_REV_GROWTH = 0.05
MB_OP_MARGIN = 0.15
MB_CAP_US = 10_000_000_000          # $10B
MB_CAP_KR = 5_000_000_000_000       # 5조 원

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


def fetch_kr_pykrx_fundamentals() -> Dict[str, Dict]:
    """Get KR fundamentals (PER/PBR/EPS/BPS/DPS/DIV) for ALL KRX stocks at once.

    yfinance's KR coverage of trailingPE/PEG is poor; pykrx pulls these directly
    from KRX (the authoritative source). One call per market gives data for ~2700
    stocks instantly. Returns dict {ticker6: {per, pbr, eps, bps, div_yield}}.
    """
    try:
        from pykrx import stock as krxstock
    except ImportError:
        print("  [pykrx] not installed — KR fundamentals will rely on yfinance only")
        return {}

    today = date.today()
    # KRX publishes after market close; try up to 10 days back to find a session w/ data
    for offset in range(0, 10):
        ds = (today - timedelta(days=offset)).strftime("%Y%m%d")
        try:
            df = krxstock.get_market_fundamental(ds, market="ALL")
            if df is None or len(df) == 0:
                continue
            print(f"  [pykrx] fundamentals: {len(df)} rows for {ds}")
            out: Dict[str, Dict] = {}
            for ticker6, row in df.iterrows():
                t6 = str(ticker6).zfill(6)
                per = float(row.get("PER", 0) or 0)
                pbr = float(row.get("PBR", 0) or 0)
                eps = float(row.get("EPS", 0) or 0)
                bps = float(row.get("BPS", 0) or 0)
                div = float(row.get("DIV", 0) or 0)  # already pct (e.g. 2.5 = 2.5%)
                out[t6] = {
                    "per": per if per > 0 else None,
                    "pbr": pbr if pbr > 0 else None,
                    "eps": eps if eps != 0 else None,
                    "bps": bps if bps != 0 else None,
                    "div_yield": (div / 100.0) if div > 0 else None,
                }
            return out
        except Exception as e:
            print(f"  [pykrx] {ds} failed: {e}")
            continue
    print("  [pykrx] all attempts failed; falling back to yfinance only for KR")
    return {}


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


def is_must_buy(metrics: Dict, market: str) -> Tuple[bool, Dict[str, bool]]:
    """Buffett-style 'wonderful company at fair price'. ALL conditions must pass."""
    roe = metrics.get("roe")
    fcf = metrics.get("fcf")
    de = metrics.get("debt_equity")
    per = metrics.get("per")
    peg = metrics.get("peg")
    rev = metrics.get("revenue_growth")
    op = metrics.get("operating_margin")
    cap = metrics.get("market_cap")
    cap_threshold = MB_CAP_KR if market == "KR" else MB_CAP_US

    checks = {
        "roe_20": (roe is not None and roe >= MB_ROE),
        "fcf_positive": (fcf is not None and fcf > 0),
        "debt_low": (de is not None and 0 <= de <= MB_DE),
        "per_fair": (per is not None and 0 < per <= MB_PER),
        "peg_attractive": (peg is not None and 0 < peg <= MB_PEG),
        "growth_solid": (rev is not None and rev >= MB_REV_GROWTH),
        "margin_strong": (op is not None and op >= MB_OP_MARGIN),
        "scale_safe": (cap is not None and cap >= cap_threshold),
    }
    return all(checks.values()), checks


def _process_one(stock: Dict, kr_fund_map: Optional[Dict[str, Dict]] = None) -> Optional[Dict]:
    info = fetch_fundamentals(stock["yf_ticker"])
    if info is None:
        return None

    # Boost KR coverage: inject pykrx PER/PBR/DIV when yfinance is missing them.
    if kr_fund_map and stock["market"] == "KR":
        kr = kr_fund_map.get(stock["ticker"])
        if kr:
            if info.get("trailingPE") is None and kr.get("per") is not None:
                info["trailingPE"] = kr["per"]
            if info.get("priceToBook") is None and kr.get("pbr") is not None:
                info["priceToBook"] = kr["pbr"]
            if info.get("dividendYield") is None and kr.get("div_yield") is not None:
                info["dividendYield"] = kr["div_yield"]

    sc = score_stock(info)
    mb_pass, mb_checks = is_must_buy(sc["metrics"], stock["market"])
    return {
        "ticker": stock["ticker"],
        "yf_ticker": stock["yf_ticker"],
        "name": stock["name"],
        "market": stock["market"],
        "score": sc["score"],
        "passes": sc["passes"],
        "metrics": sc["metrics"],
        "must_buy": mb_pass,
        "must_buy_checks": mb_checks,
        "industry": info.get("industry", "") or "",
        "sector": info.get("sector", "") or "",
        "summary": (info.get("longBusinessSummary") or "")[:300],
        "website": info.get("website", "") or "",
    }


def screen_universe(
    universe: List[Dict],
    limit: Optional[int] = None,
    kr_fund_map: Optional[Dict[str, Dict]] = None,
) -> List[Dict]:
    targets = universe[:limit] if limit else universe
    n = len(targets)
    results: List[Dict] = []
    completed = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(_process_one, s, kr_fund_map): s for s in targets}
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

    # One-shot KR fundamentals via pykrx (covers PER/PBR/DIV gap in yfinance)
    print("\n[screener] fetching KR fundamentals (pykrx) ...")
    kr_fund_map = fetch_kr_pykrx_fundamentals()

    print("\n[screener] scoring KR ...")
    kr_results = screen_universe(kr, limit=limit, kr_fund_map=kr_fund_map)
    print(f"[screener] KR: {len(kr_results)} scored")

    print("\n[screener] scoring US ...")
    us_results = screen_universe(us, limit=limit)
    print(f"[screener] US: {len(us_results)} scored")

    all_results = kr_results + us_results

    must_buy_count = sum(1 for s in all_results if s.get("must_buy"))
    output = {
        "updated_at": kst_now_iso(),
        "started_at": started_at,
        "kr_universe_size": len(kr),
        "us_universe_size": len(us),
        "kr_scored": len(kr_results),
        "us_scored": len(us_results),
        "must_buy_count": must_buy_count,
        "thresholds": {
            "roe_min": ROE_MIN,
            "debt_equity_max": DEBT_EQUITY_MAX,
            "per_max": PER_MAX,
            "peg_max": PEG_MAX,
            "revenue_growth_min": REV_GROWTH_MIN,
            "operating_margin_min": OP_MARGIN_MIN,
        },
        "must_buy_thresholds": {
            "roe_min": MB_ROE,
            "debt_equity_max": MB_DE,
            "per_max": MB_PER,
            "peg_max": MB_PEG,
            "revenue_growth_min": MB_REV_GROWTH,
            "operating_margin_min": MB_OP_MARGIN,
            "market_cap_us": MB_CAP_US,
            "market_cap_kr": MB_CAP_KR,
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
        "must_buy_labels_kr": {
            "roe_20": "ROE ≥ 20% (해자)",
            "fcf_positive": "FCF > 0",
            "debt_low": "부채비율 ≤ 60%",
            "per_fair": "PER ≤ 25 (가격 매력)",
            "peg_attractive": "PEG ≤ 1.0 (성장 대비 싸다)",
            "growth_solid": "매출 성장 ≥ 5%",
            "margin_strong": "영업이익률 ≥ 15%",
            "scale_safe": "시총 충분 (US $10B / KR 5조)",
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
        mb = "🏆" if s.get("must_buy") else "  "
        print(f"  {mb} {s['score']}/7  {s['market']}  {s['ticker']:8} {s['name'][:28]:28} cap={cap_b:6.1f}B")

    # Must-buy roll call
    mb_list = [s for s in all_results if s.get("must_buy")]
    mb_list.sort(key=lambda s: -(s["metrics"].get("market_cap") or 0))
    print(f"\n🏆 MUST-BUY (S-tier, {len(mb_list)} names):")
    for s in mb_list:
        cap_b = (s["metrics"].get("market_cap") or 0) / 1e9
        per = s["metrics"].get("per") or 0
        roe = (s["metrics"].get("roe") or 0) * 100
        print(f"     {s['market']}  {s['ticker']:8} {s['name'][:28]:28} cap={cap_b:6.1f}B  ROE={roe:5.1f}%  PER={per:5.1f}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
