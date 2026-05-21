"""Day-trading recommendation engine.

Takes a candidate that already passed initial signal detection
(from intraday_scanner) and computes a composite conviction score using
multiple intraday factors. Outputs a recommendation tier with reasoning,
suggested stop-loss, and target.

Factors (bullish unless noted):
  - Above 30-min VWAP
  - Near today's high (top 25% of range)
  - Volume burn ahead of pace
  - 5-bar momentum positive
  - Long lower wicks (buyers absorbing)
  - Gap not yet exhausted
  - Market regime supportive (KOSPI/KOSDAQ up)
  - Sector strength (proxied by snap)
  - 20-day breakout (from scanner)
  - 20-day SMA cross (from scanner)
  - RSI sweet spot (45-65)
Bearish:
  - Below VWAP
  - RSI overbought (>78)
  - Below today's open (broken support)
  - Upper-wick rejection on last few bars
  - Volume exhausted (>3× already)
"""

from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Tuple

import pytz


CONVICTION_STRONG = "🔥 강력매수"
CONVICTION_MODERATE = "⚡ 매수후보"
CONVICTION_WATCH = "👀 관찰"

WEIGHTS = {
    # bullish — chart/momentum
    "above_vwap_30m": 1.5,
    "near_today_high": 1.5,
    "volume_burn_ahead": 2.5,
    "momentum_5bar_pos": 2.0,
    "long_lower_wicks": 1.0,
    "small_gap_not_exhausted": 0.5,
    "market_regime_up": 1.0,
    "breakout_20d_high": 2.5,
    "ma20_cross": 1.5,
    "rsi_sweet_spot": 1.0,
    "rsi_oversold_exit": 1.5,
    "above_today_open": 1.0,
    # bullish — flow/supply-demand (added)
    "trade_strength_strong": 2.0,      # 체결강도 ≥ 120
    "orderbook_buy_dominance": 1.5,    # 호가 매수잔량/매도잔량 ≥ 1.5
    "last_bar_volume_spike": 1.5,      # 직전 1분봉 거래량 ≥ 직전 5봉 평균 ×3
    "near_52w_high": 1.5,              # 52주 고가의 95%↑ (매물대 적음)
    # bearish
    "below_vwap_30m": -2.0,
    "rsi_overbought": -2.0,
    "below_today_open": -2.5,
    "upper_wick_rejection": -1.0,
    "volume_exhausted_late": -1.5,
    "market_regime_down": -1.5,
    "wide_bid_ask_spread": -1.0,
    # bearish — flow/liquidity (added)
    "trade_strength_weak": -1.5,        # 체결강도 ≤ 80
    "orderbook_sell_dominance": -1.5,   # 호가 매수/매도 ≤ 0.7
    "low_liquidity": -2.0,              # 거래대금 < 50억
}

# 강력매수 등급을 막는 부정 요인 (한 개라도 있으면 매수후보로 강등)
STRONG_TIER_BLOCKERS = {
    "below_today_open",
    "low_liquidity",
    "trade_strength_weak",
    "orderbook_sell_dominance",
    "rsi_overbought",
}

LIQUIDITY_MIN_KRW = 5_000_000_000  # 50억 — 미만이면 슬리피지 리스크

# Sum of all positive weights for normalization
MAX_BULL = sum(w for w in WEIGHTS.values() if w > 0)


def _kst_now():
    return datetime.now(pytz.timezone("Asia/Seoul"))


def _session_progress() -> float:
    """Return 0~1 indicating how far we are into the regular session (09:00-15:30 KST)."""
    now = _kst_now().time()
    open_m = 9 * 60
    close_m = 15 * 60 + 30
    cur_m = now.hour * 60 + now.minute
    return max(0.0, min(1.0, (cur_m - open_m) / (close_m - open_m)))


def _compute_vwap(bars: List[dict]) -> Optional[float]:
    """VWAP from 1-min bars [{open,high,low,close,vol}, …] sorted ascending by time."""
    if not bars:
        return None
    num = 0.0
    den = 0.0
    for b in bars:
        typical = (b["high"] + b["low"] + b["close"]) / 3
        v = b["vol"]
        num += typical * v
        den += v
    if den <= 0:
        return None
    return num / den


def _momentum_5bar(bars: List[dict]) -> float:
    """Return % return from 5 bars ago to last bar, of close."""
    if len(bars) < 6:
        return 0.0
    start = bars[-6]["close"]
    end = bars[-1]["close"]
    if start <= 0:
        return 0.0
    return (end - start) / start


def _wick_pattern(bars: List[dict], lookback: int = 5) -> Tuple[float, float]:
    """Return (avg lower wick %, avg upper wick %) over last `lookback` bars.
    A long lower wick = buyers stepping in (bullish).
    A long upper wick = rejection at highs (bearish).
    """
    if not bars:
        return 0.0, 0.0
    recent = bars[-lookback:]
    lws = []
    uws = []
    for b in recent:
        rng = b["high"] - b["low"]
        if rng <= 0:
            continue
        body_low = min(b["open"], b["close"])
        body_high = max(b["open"], b["close"])
        lw = (body_low - b["low"]) / rng
        uw = (b["high"] - body_high) / rng
        lws.append(lw)
        uws.append(uw)
    if not lws:
        return 0.0, 0.0
    return sum(lws) / len(lws), sum(uws) / len(uws)


def score_candidate(
    *,
    ticker6: str,
    snap: dict,                   # current snapshot (KIS inquire-price 'output')
    indicators: dict,             # daily_indicators row (sma20, rsi14, prev_close, vol_median20, high20)
    bars_1min: List[dict],        # recent 1-min bars [{hhmm, open, high, low, close, vol}]
    market_regime_pct: float = 0.0,  # current KOSPI %change from open (or KOSDAQ)
    initial_triggers: Optional[List[str]] = None,  # signals from scanner ('20일선↑돌파' etc.)
    orderbook: Optional[dict] = None,
) -> Dict:
    """Compute composite score + conviction tier."""
    if initial_triggers is None:
        initial_triggers = []

    pos: List[Tuple[str, float, str]] = []   # (label, weight, human reason)
    neg: List[Tuple[str, float, str]] = []

    current = snap["current"]
    open_p = snap["open"]
    high_today = snap["high"]
    low_today = snap["low"]
    today_vol = snap["volume"]
    from_open = (current - open_p) / open_p if open_p > 0 else 0
    rng = max(high_today - low_today, 1e-9)

    # --- Bullish factors ---

    # VWAP (rolling 30-min)
    vwap = _compute_vwap(bars_1min)
    if vwap and current > vwap * 1.001:
        pos.append(("above_vwap_30m", WEIGHTS["above_vwap_30m"], f"30분 VWAP({vwap:.0f}) 위"))
    elif vwap and current < vwap * 0.999:
        neg.append(("below_vwap_30m", WEIGHTS["below_vwap_30m"], f"30분 VWAP({vwap:.0f}) 아래"))

    # Near today's high (top 25% of range)
    high_proximity = (current - low_today) / rng
    if high_proximity > 0.75:
        pos.append(("near_today_high", WEIGHTS["near_today_high"], f"오늘 고가 근처 ({high_proximity*100:.0f}%)"))

    # Volume burn rate
    sp = _session_progress()
    if indicators.get("vol_median20", 0) > 0 and sp > 0.05:
        vol_ratio = today_vol / indicators["vol_median20"]
        expected_ratio = sp  # by sp = 50%, should have ~50% of daily vol
        burn_ahead = vol_ratio / expected_ratio if expected_ratio > 0 else 0
        if burn_ahead >= 1.5 and vol_ratio <= 3.5:
            pos.append(("volume_burn_ahead",
                        WEIGHTS["volume_burn_ahead"],
                        f"거래량 burn ×{burn_ahead:.1f} (장 {sp*100:.0f}% 시점)"))
        elif burn_ahead > 4.0 and sp > 0.5:
            neg.append(("volume_exhausted_late",
                        WEIGHTS["volume_exhausted_late"],
                        f"거래량 이미 소진 (×{vol_ratio:.1f})"))

    # 5-bar momentum
    mom = _momentum_5bar(bars_1min)
    if mom > 0.002:  # > 0.2% in 5 min
        pos.append(("momentum_5bar_pos", WEIGHTS["momentum_5bar_pos"], f"최근 5분 {mom*100:+.2f}%"))

    # Wick pattern
    lw, uw = _wick_pattern(bars_1min, lookback=5)
    if lw > 0.35:
        pos.append(("long_lower_wicks", WEIGHTS["long_lower_wicks"], f"하단꼬리 {lw*100:.0f}% (저점 매수세)"))
    if uw > 0.4 and from_open > 0:
        neg.append(("upper_wick_rejection", WEIGHTS["upper_wick_rejection"], f"상단꼬리 {uw*100:.0f}% (고점 매도세)"))

    # Gap not exhausted (positive gap but not crazy)
    prev_close = indicators.get("prev_close", 0)
    if prev_close > 0:
        gap = (open_p - prev_close) / prev_close
        if 0.01 < gap < 0.05:
            pos.append(("small_gap_not_exhausted", WEIGHTS["small_gap_not_exhausted"],
                       f"갭 +{gap*100:.1f}% (여유 있음)"))

    # Above today's open vs below
    if current > open_p * 1.005:
        pos.append(("above_today_open", WEIGHTS["above_today_open"], f"시가 +{from_open*100:.1f}% 위"))
    elif current < open_p * 0.99:
        neg.append(("below_today_open", WEIGHTS["below_today_open"], f"시가 {from_open*100:.1f}% 아래 (지지 깨짐)"))

    # Market regime
    if market_regime_pct > 0.5:
        pos.append(("market_regime_up", WEIGHTS["market_regime_up"], f"코스피 +{market_regime_pct:.1f}%"))
    elif market_regime_pct < -0.5:
        neg.append(("market_regime_down", WEIGHTS["market_regime_down"], f"코스피 {market_regime_pct:.1f}%"))

    # RSI sweet spot vs overbought
    rsi = indicators.get("rsi14", 0)
    if 45 <= rsi <= 65:
        pos.append(("rsi_sweet_spot", WEIGHTS["rsi_sweet_spot"], f"RSI {rsi:.0f} (sweet spot)"))
    elif 0 < rsi < 35 and from_open > 0.005:
        pos.append(("rsi_oversold_exit", WEIGHTS["rsi_oversold_exit"], f"RSI {rsi:.0f} 과매도 탈출"))
    elif rsi > 78:
        neg.append(("rsi_overbought", WEIGHTS["rsi_overbought"], f"RSI {rsi:.0f} 과매수"))

    # Scanner-derived (existing signals counted as evidence)
    if "20일고가↑돌파" in initial_triggers:
        pos.append(("breakout_20d_high", WEIGHTS["breakout_20d_high"], "20일 고가 돌파"))
    if "20일선↑돌파" in initial_triggers:
        pos.append(("ma20_cross", WEIGHTS["ma20_cross"], "20일선 골든크로스"))

    # Orderbook: spread + 10단계 잔량 비율
    if orderbook:
        try:
            o1 = orderbook.get("output1", {})
            ask1 = float(o1.get("askp1", 0))
            bid1 = float(o1.get("bidp1", 0))
            if ask1 > 0 and bid1 > 0:
                spread_pct = (ask1 - bid1) / bid1 * 100
                if spread_pct > 0.3:
                    neg.append(("wide_bid_ask_spread", WEIGHTS["wide_bid_ask_spread"],
                               f"호가 spread {spread_pct:.2f}%"))
            total_bid = float(o1.get("total_bidp_rsqn", 0))
            total_ask = float(o1.get("total_askp_rsqn", 0))
            if total_bid > 0 and total_ask > 0:
                depth_ratio = total_bid / total_ask
                if depth_ratio >= 1.5:
                    pos.append(("orderbook_buy_dominance", WEIGHTS["orderbook_buy_dominance"],
                                f"호가 잔량비 {depth_ratio:.2f} (매수우세)"))
                elif depth_ratio <= 0.7:
                    neg.append(("orderbook_sell_dominance", WEIGHTS["orderbook_sell_dominance"],
                                f"호가 잔량비 {depth_ratio:.2f} (매도우세)"))
        except Exception:
            pass

    # 체결강도 (cttr) — 100 기준, 120↑ 매수세 압도, 80↓ 매도세 우세
    cttr = snap.get("cttr", 0)
    if cttr >= 120:
        pos.append(("trade_strength_strong", WEIGHTS["trade_strength_strong"],
                    f"체결강도 {cttr:.0f} (매수세 압도)"))
    elif 0 < cttr <= 80:
        neg.append(("trade_strength_weak", WEIGHTS["trade_strength_weak"],
                    f"체결강도 {cttr:.0f} (매도세 우세)"))

    # 직전 1분봉 거래량 스파이크 (진입 타이밍 신호)
    if len(bars_1min) >= 6:
        last_vol = bars_1min[-1]["vol"]
        prior_avg = sum(b["vol"] for b in bars_1min[-6:-1]) / 5
        if prior_avg > 0:
            spike = last_vol / prior_avg
            if spike >= 3.0:
                pos.append(("last_bar_volume_spike", WEIGHTS["last_bar_volume_spike"],
                            f"직전 1분봉 거래량 ×{spike:.1f}"))

    # 52주 고가 근접 (매물대 적음 → 추세 가속)
    w52_high = snap.get("w52_high", 0)
    if w52_high > 0 and current >= w52_high * 0.95:
        prox = current / w52_high * 100
        pos.append(("near_52w_high", WEIGHTS["near_52w_high"],
                    f"52주 고가 {prox:.0f}% (매물대 희박)"))

    # 거래대금 유동성 체크 (슬리피지 가드)
    trade_value = snap.get("trade_value", 0)
    if 0 < trade_value < LIQUIDITY_MIN_KRW:
        neg.append(("low_liquidity", WEIGHTS["low_liquidity"],
                    f"거래대금 {trade_value/1e8:.0f}억 (유동성 부족)"))

    # --- Compute composite score (normalize to 0-100) ---
    bull_total = sum(w for _, w, _ in pos)
    bear_total = sum(w for _, w, _ in neg)
    raw = bull_total + bear_total
    score = max(0.0, min(100.0, raw / MAX_BULL * 100))
    bull_count = len(pos)

    # --- Conviction tiering ---
    has_blocker = any(t in STRONG_TIER_BLOCKERS for t, *_ in neg)
    if score >= 65 and bull_count >= 5 and not has_blocker:
        conviction = CONVICTION_STRONG
    elif score >= 40 and bull_count >= 3:
        conviction = CONVICTION_MODERATE
    elif score >= 25:
        conviction = CONVICTION_WATCH
    else:
        conviction = None  # skip

    # --- Stop & target suggestions ---
    suggested_stop_pct = None
    suggested_target_pct = None
    if conviction:
        # Stop: just below today's low or -1.5%, whichever is tighter (smaller loss)
        stop_low = (low_today - current) / current  # negative
        suggested_stop_pct = max(stop_low, -0.025)  # cap at -2.5%
        # Target: next resistance = 20d high or +3%
        h20 = indicators.get("high20", 0)
        target_pcts = [0.03]
        if h20 > current:
            target_pcts.append((h20 - current) / current)
        suggested_target_pct = min(target_pcts)  # nearest target

    return {
        "ticker6": ticker6,
        "score": round(score, 1),
        "conviction": conviction,
        "factors_pos": pos,
        "factors_neg": neg,
        "bull_count": bull_count,
        "bear_count": len(neg),
        "suggested_stop_pct": suggested_stop_pct,
        "suggested_target_pct": suggested_target_pct,
        "current_price": current,
    }


def format_recommendation(rec: Dict, name: str, market_cap: float, snap: dict) -> str:
    """Build a Telegram-friendly multi-line recommendation block."""
    if not rec["conviction"]:
        return ""

    cap = market_cap
    cap_s = f"{cap/1e12:.1f}조" if cap >= 1e12 else f"{cap/1e8:,.0f}억"
    cur = rec["current_price"]
    cur_s = f"{int(cur):,}원" if cur >= 1000 else f"{cur:.0f}원"

    from_open = (cur - snap["open"]) / snap["open"] * 100 if snap["open"] > 0 else 0
    prdy = snap.get("prdy_diff_pct", 0)

    lines = [
        f"{rec['conviction']}  **{name}** ({rec['ticker6']})",
        f"   가격 {cur_s} | 전일 {prdy:+.2f}% | 시가대비 {from_open:+.2f}% | 시총 {cap_s}",
        f"   점수 {rec['score']:.0f}/100  (찬성 {rec['bull_count']}건 / 반대 {rec['bear_count']}건)",
    ]

    # Top 4 positive factors
    pos_sorted = sorted(rec["factors_pos"], key=lambda x: -x[1])
    if pos_sorted:
        lines.append("   ✅ 근거: " + " · ".join(p[2] for p in pos_sorted[:4]))
    neg_sorted = sorted(rec["factors_neg"], key=lambda x: x[1])
    if neg_sorted:
        lines.append("   ⚠️ 주의: " + " · ".join(n[2] for n in neg_sorted[:3]))

    # Stop/target if available
    stop = rec.get("suggested_stop_pct")
    target = rec.get("suggested_target_pct")
    if stop is not None and target is not None:
        stop_price = cur * (1 + stop)
        target_price = cur * (1 + target)
        rr = abs(target / stop) if stop != 0 else 0
        lines.append(
            f"   🎯 진입 {cur_s} | 손절 {int(stop_price):,}원 ({stop*100:+.1f}%) | "
            f"목표 {int(target_price):,}원 ({target*100:+.1f}%) | R:R {rr:.1f}"
        )
    return "\n".join(lines)
