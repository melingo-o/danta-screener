"""Format and send Telegram messages."""

import json
import os
import urllib.request
from datetime import datetime
from typing import List

import pandas as pd
import pytz

from model import Pick
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
        lines.append("🎯 후보 (예상 익일 수익률 + 근거):")
        for i, p in enumerate(picks, 1):
            cap_s = _fmt_cap(p.market_cap)
            vol_s = _fmt_vol_ratio(p.volume_ratio)
            lines.append(f"{i}. {p.name} ({p.ticker6}) — 시총 {cap_s}, {vol_s}")
            drivers_str = ", ".join(
                f"{us}(β={beta:+.2f}, 기여 {contrib * 100:+.2f}%p)"
                for us, beta, contrib in p.drivers
            )
            lines.append(f"   예상 {_fmt_pct(p.expected_return)} | 드라이버: {drivers_str}")
    lines.append("")

    lines.append("⚠️ 통계 모델 스크리닝. 매매 추천 아님. 호가/뉴스 직접 확인 필수.")
    return "\n".join(lines)


def send_telegram(text: str) -> dict:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
    parsed = json.loads(body)
    if not parsed.get("ok"):
        raise RuntimeError(f"Telegram send failed: {body}")
    return parsed


def kst_today_str() -> str:
    return datetime.now(pytz.timezone("Asia/Seoul")).strftime("%Y-%m-%d")
