"""Intraday scanner — every 5-10 min during market hours.

Pre-filters by simple breakout/MA/volume signals from cached daily indicators,
then for each candidate runs the recommender (composite scoring + conviction tier).
Sends only ⚡ MODERATE or 🔥 STRONG picks to Telegram (👀 WATCH logged only).
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

from kis import KISClient, KOSPI
from recommender import (
    CONVICTION_MODERATE,
    CONVICTION_STRONG,
    CONVICTION_WATCH,
    format_recommendation,
    score_candidate,
)
from telegram_client import send_telegram

INDICATORS_CSV = Path("data/daily_indicators.csv")
STATE_FILE = Path("data/scanner_state.json")

VOL_SURGE_RATIO = 2.0
PRICE_SURGE_PCT = 2.5
MIN_MARKET_CAP_KRW = 300_000_000_000  # 3,000억
MAX_DEEP_ANALYSIS = 15   # cap deep-analysis calls per tick (KIS API budget)
MAX_ALERTS_PER_RUN = 8


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


def _ff(v, default=0.0):
    try:
        s = (v or "").strip() if isinstance(v, str) else v
        if s in ("", None, "nan"):
            return default
        return float(s)
    except Exception:
        return default


def load_indicators() -> Dict[str, dict]:
    if not INDICATORS_CSV.exists():
        return {}
    out = {}
    with INDICATORS_CSV.open("r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            t = (row.get("ticker6") or "").strip()
            if not t:
                continue
            try:
                out[t] = {
                    "name": row.get("name", ""),
                    "market_cap": _ff(row.get("market_cap")),
                    "sma5": _ff(row.get("sma5")),
                    "sma20": _ff(row.get("sma20")),
                    "sma60": _ff(row.get("sma60")),
                    "rsi14": _ff(row.get("rsi14")),
                    "vol_median20": _ff(row.get("vol_median20")),
                    "prev_close": _ff(row.get("prev_close")),
                    "high20": _ff(row.get("high20")),
                    "macd_state": (row.get("macd_state") or "").strip(),
                    "chg5d_pct": _ff(row.get("chg5d_pct")),
                }
            except Exception:
                continue
    return out


def detect_initial_triggers(ind: dict, snap: dict) -> List[str]:
    """Cheap pre-filter — just enough to short-list candidates for deep analysis."""
    triggers = []
    cur = snap["current"]
    open_p = snap["open"]
    vol = snap["volume"]

    if ind["sma20"] > 0 and ind["prev_close"] > 0:
        if ind["prev_close"] < ind["sma20"] <= cur:
            triggers.append("20일선↑돌파")

    if 0 < ind["rsi14"] < 35 and cur > ind["prev_close"] * 1.01:
        triggers.append(f"RSI탈출({ind['rsi14']:.0f})")

    if ind["vol_median20"] > 0 and open_p > 0:
        vol_ratio = vol / ind["vol_median20"]
        price_chg = (cur - open_p) / open_p * 100
        if vol_ratio >= VOL_SURGE_RATIO and price_chg >= PRICE_SURGE_PCT:
            triggers.append(f"거래량×{vol_ratio:.1f}+{price_chg:+.1f}%")

    if ind["high20"] > 0 and cur > ind["high20"]:
        triggers.append("20일고가↑돌파")

    return triggers


def fetch_snapshot(client: KISClient, ticker6: str) -> Optional[dict]:
    try:
        out = client.get_stock_price(ticker6)
    except Exception as e:
        print(f"  [{ticker6}] price fetch failed: {e}")
        return None

    def _f(k, default=0.0):
        try:
            v = out.get(k, default)
            return float(v) if v not in (None, "") else default
        except Exception:
            return default

    try:
        return {
            "current": _f("stck_prpr"),
            "open": _f("stck_oprc"),
            "high": _f("stck_hgpr"),
            "low": _f("stck_lwpr"),
            "volume": _f("acml_vol"),
            "trade_value": _f("acml_tr_pbmn"),   # 누적 거래대금 (원)
            "prdy_diff_pct": _f("prdy_ctrt"),
            "w52_high": _f("w52_hgpr"),
            "w52_low": _f("w52_lwpr"),
            "per": _f("per"),
            "pbr": _f("pbr"),
            "vol_tnrt": _f("vol_tnrt"),
            "cttr": _f("cttr"),                  # 체결강도 (100기준)
            "ssts_yn": (out.get("ssts_yn") or "").strip(),
            "mrkt_warn_cls_code": (out.get("mrkt_warn_cls_code") or "").strip(),
            "invt_caful_yn": (out.get("invt_caful_yn") or "").strip(),
        }
    except Exception as e:
        print(f"  [{ticker6}] parse failed: {e}")
        return None


def fetch_1min_bars(client: KISClient, ticker6: str) -> List[dict]:
    now = kst_now()
    hhmmss = now.strftime("%H%M%S")
    try:
        raw_bars = client.get_intraday_bars(ticker6, end_hhmmss=hhmmss)
    except Exception as e:
        print(f"  [{ticker6}] bars fetch failed: {e}")
        return []
    bars = []
    for b in raw_bars:
        try:
            t = b.get("stck_cntg_hour", "")
            if not t or len(t) < 4:
                continue
            bars.append({
                "hhmm": int(t[:4]),
                "open": float(b["stck_oprc"]),
                "high": float(b["stck_hgpr"]),
                "low": float(b["stck_lwpr"]),
                "close": float(b["stck_prpr"]),
                "vol": float(b.get("cntg_vol", 0) or 0),
            })
        except Exception:
            continue
    bars.sort(key=lambda x: x["hhmm"])
    return bars


def fetch_market_regime(client: KISClient) -> float:
    """Return KOSPI % change from today's open."""
    try:
        out = client.get_index_price(KOSPI)
        cur = float(out["bstp_nmix_prpr"])
        op = float(out["bstp_nmix_oprc"])
        return (cur - op) / op * 100 if op > 0 else 0
    except Exception:
        return 0.0


def main():
    indicators = load_indicators()
    if not indicators:
        print("[scanner] no daily indicators yet — skipping (run morning_picks first)")
        return

    candidates = {t: ind for t, ind in indicators.items() if ind["market_cap"] >= MIN_MARKET_CAP_KRW}
    print(f"[scanner] universe: {len(candidates)} stocks (시총 ≥ {MIN_MARKET_CAP_KRW/1e8:,.0f}억)")

    state = load_state()
    today = kst_now().strftime("%Y-%m-%d")
    if state.get("date") != today:
        state = {"date": today}
    alerted = set(state.get("alerted", []))

    client = KISClient()
    regime = fetch_market_regime(client)
    print(f"[scanner] market regime: KOSPI 시가대비 {regime:+.2f}%")

    pre_filtered = []
    skipped_caution = 0
    for ticker6, ind in candidates.items():
        if ticker6 in alerted:
            continue
        snap = fetch_snapshot(client, ticker6)
        if not snap:
            continue
        # 시장경고(02=경고, 03=위험) / 투자유의 → 강제 제외
        # ssts_yn은 KIS에서 "공매도 거래 가능 여부"라 정상 종목 대부분이 Y → 필터에서 제거
        warn = snap.get("mrkt_warn_cls_code", "")
        if warn in ("02", "03") or snap.get("invt_caful_yn") == "Y":
            skipped_caution += 1
            continue
        triggers = detect_initial_triggers(ind, snap)
        if not triggers:
            continue
        pre_filtered.append({"ticker6": ticker6, "name": ind["name"], "ind": ind,
                              "snap": snap, "triggers": triggers})
        time.sleep(0.05)
        if len(pre_filtered) >= MAX_DEEP_ANALYSIS:
            break

    print(f"[scanner] pre-filtered: {len(pre_filtered)} (skipped {skipped_caution} 관리·위험·공매도과열)")

    deep_recs = []
    for c in pre_filtered:
        ticker6 = c["ticker6"]
        bars = fetch_1min_bars(client, ticker6)
        orderbook = None
        try:
            orderbook = client.get_orderbook(ticker6)
        except Exception:
            pass

        rec = score_candidate(
            ticker6=ticker6,
            snap=c["snap"],
            indicators=c["ind"],
            bars_1min=bars,
            market_regime_pct=regime,
            initial_triggers=c["triggers"],
            orderbook=orderbook,
        )
        rec["name"] = c["name"]
        rec["market_cap"] = c["ind"]["market_cap"]
        rec["snap"] = c["snap"]
        deep_recs.append(rec)
        time.sleep(0.05)

    # Rank by score
    deep_recs.sort(key=lambda r: -r["score"])
    print(f"[scanner] scored {len(deep_recs)}; top scores: " +
          ", ".join(f"{r['ticker6']}({r['score']:.0f})" for r in deep_recs[:5]))

    # Build alerts: only STRONG + MODERATE
    alerts = []
    for rec in deep_recs:
        if rec["conviction"] in (CONVICTION_STRONG, CONVICTION_MODERATE):
            alerts.append(rec)
            alerted.add(rec["ticker6"])
        if len(alerts) >= MAX_ALERTS_PER_RUN:
            break

    state["alerted"] = sorted(alerted)
    save_state(state)

    if not alerts:
        print("[scanner] no STRONG/MODERATE recommendations")
        return

    now_str = kst_now().strftime("%H:%M")
    n_strong = sum(1 for r in alerts if r["conviction"] == CONVICTION_STRONG)
    header = (
        f"📡 장중 추천 [{now_str} KST]  "
        f"강력매수 {n_strong}건 / 매수후보 {len(alerts)-n_strong}건\n"
        f"코스피 시가대비 {regime:+.2f}%\n"
    )
    blocks = [format_recommendation(rec, rec["name"], rec["market_cap"], rec["snap"]) for rec in alerts]
    msg = header + "\n" + "\n\n".join(blocks) + "\n\n⚠️ 통계 추천. 호가창 두께와 뉴스 직접 확인 후 진입. 손절가 지킬 것."

    print("--- recommendation alert ---")
    print(msg)
    print("----------------------------")
    send_telegram(msg)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
