"""Format and send Telegram messages."""

from datetime import datetime
from typing import List

import pandas as pd
import pytz

from model import Pick
from telegram_client import send_telegram
from universe import (
    FINVIZ_MAP_URL,
    FINVIZ_SECTOR_URL,
    KR_MARKET_PROXY,
    RISK_OFF_THRESHOLD_PCT,
    SECTORS,
    US_MOVE_THRESHOLD_PCT,
)


def _fmt_cap(cap_krw: float) -> str:
    조 = 1_000_000_000_000
    억 = 100_000_000
    if cap_krw >= 조:
        return f"{cap_krw / 조:.1f}조"
    return f"{int(cap_krw / 억):,}억"


def _fmt_vol_ratio(vr: float) -> str:
    if vr != vr:  # NaN
        return "거래량 N/A"
    return f"거래량 ×{vr:.1f}"


def _fmt_pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def _sector_blocks(last_us: pd.Series) -> List[str]:
    """Build per-sector text blocks. Show only sectors with at least one |move| >= threshold."""
    thresh = US_MOVE_THRESHOLD_PCT / 100.0
    blocks = []
    for sect in SECTORS:
        etf = sect["etf"]
        stocks = sect["stocks"]
        all_tickers = ([etf] if etf else []) + list(stocks)
        present = [t for t in all_tickers if t in last_us.index]
        if not present:
            continue
        has_significant = any(abs(last_us[t]) >= thresh for t in present)
        if not has_significant:
            continue

        # Header: sector name + ETF move (if ETF available)
        header = sect["name"]
        if etf and etf in last_us.index:
            header = f"{sect['name']} ({etf} {_fmt_pct(last_us[etf])})"

        # Body: list each constituent stock with its move; sort by |move| desc
        stock_rows = []
        for s in stocks:
            if s in last_us.index:
                stock_rows.append((s, last_us[s]))
        stock_rows.sort(key=lambda x: abs(x[1]), reverse=True)
        body_lines = [f"  • {s} {_fmt_pct(r)}" for s, r in stock_rows]

        block = header
        if body_lines:
            block += "\n" + "\n".join(body_lines)
        blocks.append(block)
    return blocks


def _pick_reason(pick, last_us: pd.Series) -> str:
    """One-line plain-language reason for why this pick was selected.

    Combines: top-1/2 contributing US driver overnight moves +
    the sector that primary driver belongs to + primary beta.
    Helps the user understand intuitively what theme/sector drove the pick
    without having to parse raw β/contribution numbers.
    """
    if not pick.drivers:
        return ""

    sorted_drivers = sorted(pick.drivers, key=lambda d: abs(d[2]), reverse=True)
    top = sorted_drivers[:2]

    primary_us, primary_beta, _ = top[0]
    sector_name = None
    for sect in SECTORS:
        if primary_us == sect.get("etf") or primary_us in sect.get("stocks", []):
            sector_name = sect["name"]
            break

    moves_str = " · ".join(
        f"{us} {last_us.get(us, 0) * 100:+.1f}%" for us, _, _ in top
    )
    if sector_name:
        tail = f"{sector_name} 섹터 β{primary_beta:+.2f}로 가장 강하게 동조"
    else:
        tail = f"동조 베타 β{primary_beta:+.2f}로 가장 큼"

    return f"{moves_str} → {tail}"


def _regime_warning(last_us: pd.Series) -> str | None:
    """Return a warning string if Korea market proxy moved sharply, else None."""
    if KR_MARKET_PROXY not in last_us.index:
        return None
    move = last_us[KR_MARKET_PROXY]
    if abs(move) * 100 < RISK_OFF_THRESHOLD_PCT:
        return None
    direction = "리스크오프 (한국 갭다운 유력)" if move < 0 else "리스크온 (한국 갭업 유력)"
    return f"⚠️ EWY {_fmt_pct(move)} → 광역 {direction}. 개별 픽보다 시장 방향 우선 고려."


def format_message(picks: List[Pick], last_us: pd.Series, kst_date: str) -> str:
    lines = [f"📊 [{kst_date} 단타 후보]", ""]

    warning = _regime_warning(last_us)
    if warning:
        lines.append(warning)
        lines.append("")

    lines.append("🇺🇸 어젯밤 미장 (섹터별):")
    blocks = _sector_blocks(last_us)
    if blocks:
        for b in blocks:
            lines.append("")
            lines.append(b)
    else:
        lines.append("  (큰 움직임 없음)")
    lines.append("")
    lines.append(f"📎 섹터 히트맵: {FINVIZ_SECTOR_URL}")
    lines.append(f"📎 종목 트리맵: {FINVIZ_MAP_URL}")
    lines.append("")

    if not picks:
        lines.append("🎯 후보:")
        lines.append("오늘은 통계적 신호가 약함. 관망 추천.")
    else:
        lines.append("🎯 후보 (예상 시초가 갭 + 근거):")
        lines.append("   ※ 시초가 갭 = 어제 종가 → 오늘 9:00 시가 변동률")
        lines.append("   ※ 순(net) = 0.5% 단타 비용(슬리피지+수수료+세) 차감 후")
        for i, p in enumerate(picks, 1):
            cap_s = _fmt_cap(p.market_cap)
            vol_s = _fmt_vol_ratio(p.volume_ratio)
            lines.append(f"{i}. {p.name} ({p.ticker6}) — 시총 {cap_s}, {vol_s}")
            lines.append(
                f"   📈 예상 갭 {_fmt_pct(p.expected_return)} "
                f"(순 {_fmt_pct(p.expected_return_net)})"
            )
            reason = _pick_reason(p, last_us)
            if reason:
                lines.append(f"   💡 {reason}")
            drivers_str = ", ".join(
                f"{us}(β{beta:+.2f}, 기여{contrib * 100:+.2f}%p)"
                for us, beta, contrib in p.drivers
            )
            lines.append(f"   🔧 드라이버 상세: {drivers_str}")
    lines.append("")

    lines.append("⚠️ 통계 모델 스크리닝. 매매 추천 아님. 호가/뉴스 직접 확인 필수.")
    lines.append("    9:00 시가 갭 예측이라 9:00~9:30 단타에 최적화됨.")
    return "\n".join(lines)


def kst_today_str() -> str:
    return datetime.now(pytz.timezone("Asia/Seoul")).strftime("%Y-%m-%d")
