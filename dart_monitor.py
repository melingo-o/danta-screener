"""DART (전자공시) monitor.

Every 10 min during market hours: fetch new filings, filter by keywords +
market-cap floor, send Telegram alert with company / title / dart link.

Needs DART_API_KEY env var (free, from https://opendart.fss.or.kr/).
State (already-seen rcept_no list for today) in data/dart_state.json.
"""

import csv
import json
import os
import sys
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pytz

from telegram_client import send_telegram

DART_API = "https://opendart.fss.or.kr/api/list.json"
DART_VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="

STATE_FILE = Path("data/dart_state.json")
UNIVERSE_CSV = Path("data/universe.csv")

# Keywords that historically correlate with short-term price spikes
BULLISH_KEYWORDS = [
    "수주", "공급계약", "단일판매·공급계약", "공급 계약",
    "자기주식취득", "자사주매입", "자사주 매입",
    "무상증자", "주식분할",
    "합병", "분할", "주식교환",
    "신주인수권", "전환사채",
    "흑자전환", "잠정실적", "매출액 또는 손익구조",
    "신약", "임상", "품목허가", "FDA",
    "특허", "기술이전", "라이센스",
    "MOU", "양해각서", "협력",
    "투자", "유상증자",
    "최대주주변경", "경영권",
]

# Bearish-but-noteworthy
BEARISH_KEYWORDS = [
    "감자", "상장폐지", "관리종목",
    "횡령", "배임",
    "주식발행한도",
]

ALL_KEYWORDS = BULLISH_KEYWORDS + BEARISH_KEYWORDS
MIN_MARKET_CAP_KRW = 100_000_000_000  # 1000억 (relax for early-stage news)


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


def load_universe_map() -> Dict[str, dict]:
    """corp 6-digit code → {name, market_cap}. From data/universe.csv (built by morning_picks)."""
    out = {}
    if not UNIVERSE_CSV.exists():
        return out
    with UNIVERSE_CSV.open("r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            # Note: universe.csv from FDR has 'code' as index name, but read as 'code'
            code = (row.get("code") or "").strip().zfill(6)
            if not code:
                continue
            try:
                cap = float(row.get("Marcap", 0) or 0)
            except Exception:
                cap = 0
            out[code] = {
                "name": row.get("Name", ""),
                "market_cap": cap,
                "market": row.get("Market", ""),
            }
    return out


def fetch_filings(api_key: str, bgn_de: str, end_de: str, max_pages: int = 3) -> List[dict]:
    """Fetch all filings between bgn_de and end_de (YYYYMMDD). Up to 100/page × max_pages."""
    out = []
    page = 1
    while page <= max_pages:
        params = {
            "crtfc_key": api_key,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "page_no": str(page),
            "page_count": "100",
        }
        url = DART_API + "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"[dart] page {page} fetch failed: {e}")
            break
        if data.get("status") not in ("000",):
            print(f"[dart] API status {data.get('status')}: {data.get('message')}")
            break
        rows = data.get("list", []) or []
        if not rows:
            break
        out.extend(rows)
        total_page = int(data.get("total_page", 1) or 1)
        if page >= total_page:
            break
        page += 1
    return out


def match_keywords(title: str) -> List[str]:
    return [kw for kw in ALL_KEYWORDS if kw in title]


def keyword_emoji(kws: List[str]) -> str:
    if any(k in kws for k in BULLISH_KEYWORDS):
        return "📈"
    return "📉"


def main():
    api_key = os.environ.get("DART_API_KEY", "").strip()
    if not api_key:
        print("[dart] DART_API_KEY not set — skipping. Add via gh secret set DART_API_KEY")
        return

    now = kst_now()
    today_yyyymmdd = now.strftime("%Y%m%d")
    state = load_state()
    if state.get("date") != today_yyyymmdd:
        state = {"date": today_yyyymmdd}
    seen = set(state.get("seen", []))

    filings = fetch_filings(api_key, today_yyyymmdd, today_yyyymmdd)
    print(f"[dart] fetched {len(filings)} filings for {today_yyyymmdd}")

    if not filings:
        save_state(state)
        return

    universe = load_universe_map()
    print(f"[dart] universe map loaded: {len(universe)} stocks")

    alerts = []
    for f in filings:
        rcept_no = (f.get("rcept_no") or "").strip()
        if not rcept_no or rcept_no in seen:
            continue
        title = (f.get("report_nm") or "").strip()
        corp_code = (f.get("stock_code") or "").strip()
        corp_name = (f.get("corp_name") or "").strip()

        if not corp_code or len(corp_code) != 6:
            continue  # filter to listed companies (have stock_code)

        # Keyword match
        kws = match_keywords(title)
        if not kws:
            continue

        # Market cap filter (if we know the cap)
        uni = universe.get(corp_code)
        if uni:
            if uni["market_cap"] < MIN_MARKET_CAP_KRW:
                continue
            cap_s = (
                f"{uni['market_cap']/1e12:.1f}조"
                if uni["market_cap"] >= 1e12
                else f"{uni['market_cap']/1e8:,.0f}억"
            )
        else:
            cap_s = "시총 N/A"

        rcept_dt = (f.get("rcept_dt") or "").strip()
        filing_time = (f.get("rcept_no") or "")[8:12]  # 일부 시각 정보 추정

        alerts.append({
            "rcept_no": rcept_no,
            "corp_name": corp_name,
            "corp_code": corp_code,
            "title": title,
            "kws": kws,
            "cap_s": cap_s,
            "emoji": keyword_emoji(kws),
        })
        seen.add(rcept_no)

    state["seen"] = sorted(seen)
    save_state(state)

    if not alerts:
        print("[dart] no new keyword-matched filings")
        return

    now_hhmm = now.strftime("%H:%M")
    lines = [f"📋 DART 공시 [{now_hhmm} KST] — {len(alerts)}건"]
    for a in alerts[:15]:  # cap at 15 per message
        lines.append(
            f"\n{a['emoji']} **{a['corp_name']}** ({a['corp_code']}, {a['cap_s']})"
            f"\n   {a['title']}"
            f"\n   🔗 {DART_VIEWER}{a['rcept_no']}"
            f"\n   키워드: {', '.join(a['kws'])}"
        )

    msg = "\n".join(lines)
    print("--- dart alert ---")
    print(msg)
    print("------------------")
    send_telegram(msg)
    print(f"[dart] sent {len(alerts)} alerts")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
