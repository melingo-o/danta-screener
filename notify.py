"""Format and send Telegram messages."""

import json
import os
import urllib.request
from datetime import datetime
from typing import List

import pandas as pd
import pytz

from model import Pick


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


def format_message(picks: List[Pick], last_us: pd.Series, kst_date: str) -> str:
    lines = [f"📊 [{kst_date} 단타 후보]", ""]

    # Big US movers
    movers = last_us.reindex(last_us.abs().sort_values(ascending=False).index)
    movers = movers[movers.abs() >= 0.015].head(8)
    if len(movers) > 0:
        lines.append("🇺🇸 어젯밤 미장 큰 움직임:")
        for t, r in movers.items():
            sign = "+" if r > 0 else ""
            lines.append(f"- {t} {sign}{r * 100:.1f}%")
        lines.append("")

    if not picks:
        lines.append("🎯 후보:")
        lines.append("오늘은 통계적 신호가 약함. 관망 추천.")
        lines.append("")
    else:
        lines.append("🎯 후보 (예상 익일 수익률 + 근거):")
        for i, p in enumerate(picks, 1):
            sign = "+" if p.expected_return >= 0 else ""
            cap_s = _fmt_cap(p.market_cap)
            vol_s = _fmt_vol_ratio(p.volume_ratio)
            lines.append(f"{i}. {p.name} ({p.ticker6}) — 시총 {cap_s}, {vol_s}")
            lines.append(
                f"   예상 {sign}{p.expected_return * 100:.2f}% | 드라이버: "
                + ", ".join(
                    f"{us}(β={beta:+.2f}, 기여 {contrib * 100:+.2f}%p)"
                    for us, beta, contrib in p.drivers
                )
            )
        lines.append("")

    lines.append("⚠️ 통계 모델 스크리닝. 매매 추천 아님. 호가/뉴스 직접 확인 필수.")
    return "\n".join(lines)


def send_telegram(text: str) -> dict:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
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
