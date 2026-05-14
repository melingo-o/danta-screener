"""News monitor — 시장 속보 RSS + 단타 후보 종목별 네이버 뉴스.

Runs alongside dart_monitor every 5 min from intraday_master. Two sources:

1. 시장 전체 속보 — 한경/연합 경제 RSS, keyword-filtered for market-moving
   headlines (금리·환율·지정학·서킷브레이커 등).
2. 종목 뉴스 — for each ticker in today's scanner_state.alerted, scrape
   finance.naver.com item news page, filter by bullish/bearish keywords.

State: data/news_state.json — daily-reset, holds seen guid/article-id sets so
the same headline never re-alerts within a session.
"""

import csv
import json
import re
import sys
import time
import traceback
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import List, Optional, Tuple

import pytz

from telegram_client import send_telegram

STATE_FILE = Path("data/news_state.json")
SCANNER_STATE = Path("data/scanner_state.json")
INDICATORS_CSV = Path("data/daily_indicators.csv")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0 Safari/537.36"
)

# --- 시장 전체 속보 (RSS) ---
MARKET_RSS = [
    ("한경증권", "https://www.hankyung.com/feed/finance"),
    ("연합뉴스경제", "https://www.yna.co.kr/rss/economy.xml"),
]

# Headlines worth waking us up for — broad market or sector-wide impact.
MARKET_KEYWORDS = [
    "FOMC", "기준금리", "금리인하", "금리인상", "긴축", "양적완화",
    "환율", "원달러", "달러강세", "달러약세",
    "급락", "급등", "폭락", "폭등", "패닉",
    "서킷브레이커", "사이드카",
    "북한", "지정학", "전쟁", "휴전",
    "관세", "보복관세", "무역분쟁",
    "유가급등", "유가급락",
    "엔비디아", "반도체 사이클",
    "코스피 사상", "코스닥 사상", "최고치 경신", "최저치",
]

# --- 종목 뉴스 키워드 ---
STOCK_BULLISH = [
    "수주", "공급계약", "납품", "MOU", "협약체결",
    "신약", "임상", "FDA", "품목허가", "특허취득",
    "흑자전환", "최대실적", "어닝서프라이즈", "사상최대",
    "자사주매입", "자기주식취득", "무상증자", "주식분할",
    "투자유치", "지분인수", "M&A",
    "수출", "1조", "신고가",
]
STOCK_BEARISH = [
    "급락", "하한가", "감자", "상장폐지", "관리종목",
    "거래정지", "횡령", "배임", "분식회계",
    "유상증자", "전환사채", "BW", "신주인수권",
    "압수수색", "검찰", "조사",
    "리콜", "결함",
]

MAX_MARKET_ALERTS_PER_RUN = 5
MAX_STOCK_ALERTS_PER_RUN = 8
MAX_TICKERS_TO_POLL = 25       # cap Naver scrapes per tick (rate-limit guard)
FETCH_SLEEP_SEC = 0.15         # politeness delay between Naver requests


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


def _http_get(url: str, timeout: int = 10) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except Exception as e:
        print(f"  http_get failed [{url[:70]}]: {e}")
        return None
    for enc in ("utf-8", "euc-kr"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


# ---------------- 시장 속보 ----------------

def parse_rss_items(xml_text: str) -> List[dict]:
    """RSS 2.0 → [{title, link, guid, pubdate}, …]."""
    items: List[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        print(f"  rss parse failed: {e}")
        return items
    for chan in root.findall(".//channel"):
        for it in chan.findall("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            guid = (it.findtext("guid") or link).strip()
            pub = (it.findtext("pubDate") or "").strip()
            if title:
                items.append({"title": title, "link": link, "guid": guid, "pubdate": pub})
    return items


def market_keyword_hit(title: str) -> Optional[str]:
    for kw in MARKET_KEYWORDS:
        if kw in title:
            return kw
    return None


def fetch_market_news(seen: set) -> List[Tuple[str, dict, str]]:
    new_items: List[Tuple[str, dict, str]] = []
    for source, url in MARKET_RSS:
        text = _http_get(url)
        if not text:
            continue
        for item in parse_rss_items(text):
            uid = item["guid"] or item["link"]
            if not uid or uid in seen:
                continue
            kw = market_keyword_hit(item["title"])
            if not kw:
                seen.add(uid)  # mark non-matching so we don't re-evaluate
                continue
            new_items.append((source, item, kw))
            seen.add(uid)
            if len(new_items) >= MAX_MARKET_ALERTS_PER_RUN:
                break
        if len(new_items) >= MAX_MARKET_ALERTS_PER_RUN:
            break
    return new_items


# ---------------- 종목 뉴스 ----------------

NAVER_ITEM_NEWS_URL = (
    "https://finance.naver.com/item/news_news.naver?code={code}"
    "&page=1&sm=title_entity_id.basic&clusterId="
)
_NEWS_ROW_RE = re.compile(
    r'<a[^>]+href="(?P<href>/item/news_read\.naver\?[^"]+)"[^>]*>\s*'
    r'(?:<span[^>]*>)?\s*(?P<title>[^<]+?)\s*(?:</span>)?\s*</a>',
    re.IGNORECASE,
)
_ARTICLE_ID_RE = re.compile(r"article_id=(\d+)")
_OFFICE_ID_RE = re.compile(r"office_id=(\d+)")


def parse_stock_news_html(html: str) -> List[dict]:
    out: List[dict] = []
    seen_uids = set()
    for m in _NEWS_ROW_RE.finditer(html):
        href = unescape(m.group("href"))
        title = unescape(m.group("title")).strip()
        if not title:
            continue
        aid = _ARTICLE_ID_RE.search(href)
        oid = _OFFICE_ID_RE.search(href)
        if not aid or not oid:
            continue
        uid = f"{oid.group(1)}_{aid.group(1)}"
        if uid in seen_uids:
            continue
        seen_uids.add(uid)
        full_url = "https://finance.naver.com" + href.replace("&amp;", "&")
        out.append({"uid": uid, "title": title, "url": full_url})
    return out


def stock_news_keyword(title: str) -> Tuple[Optional[str], str]:
    for kw in STOCK_BEARISH:
        if kw in title:
            return kw, "bear"
    for kw in STOCK_BULLISH:
        if kw in title:
            return kw, "bull"
    return None, ""


def fetch_stock_news_one(
    ticker6: str, name: str, seen_ids: set
) -> List[Tuple[str, str, str, str, str, str]]:
    """Return [(ticker6, name, title, url, kw, direction), …] for fresh keyword hits."""
    html = _http_get(NAVER_ITEM_NEWS_URL.format(code=ticker6), timeout=8)
    if not html:
        return []
    hits: List[Tuple[str, str, str, str, str, str]] = []
    for r in parse_stock_news_html(html):
        if r["uid"] in seen_ids:
            continue
        kw, direction = stock_news_keyword(r["title"])
        seen_ids.add(r["uid"])
        if not kw:
            continue
        hits.append((ticker6, name, r["title"], r["url"], kw, direction))
    return hits


def load_alerted_tickers() -> List[Tuple[str, str]]:
    """Today's scanner alerts → [(ticker6, name)]. Name resolved from daily_indicators.csv."""
    if not SCANNER_STATE.exists():
        return []
    try:
        st = json.loads(SCANNER_STATE.read_text(encoding="utf-8"))
        alerted = st.get("alerted", [])
    except Exception:
        return []

    name_map = {}
    if INDICATORS_CSV.exists():
        with INDICATORS_CSV.open("r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                t = (row.get("ticker6") or "").strip()
                if t:
                    name_map[t] = row.get("name", "")
    return [(t, name_map.get(t, "")) for t in alerted[:MAX_TICKERS_TO_POLL]]


# ---------------- main ----------------

def main():
    now = kst_now()
    today = now.strftime("%Y-%m-%d")
    state = load_state()
    if state.get("date") != today:
        state = {"date": today, "market_seen": [], "stock_seen": []}

    market_seen = set(state.get("market_seen", []))
    stock_seen = set(state.get("stock_seen", []))

    # 1) 시장 속보
    market_hits = fetch_market_news(market_seen)

    # 2) 단타 후보 종목별 뉴스
    alerted = load_alerted_tickers()
    stock_hits: List[Tuple[str, str, str, str, str, str]] = []
    for ticker6, name in alerted:
        hits = fetch_stock_news_one(ticker6, name, stock_seen)
        stock_hits.extend(hits)
        if len(stock_hits) >= MAX_STOCK_ALERTS_PER_RUN:
            break
        time.sleep(FETCH_SLEEP_SEC)

    state["market_seen"] = sorted(market_seen)[-200:]
    state["stock_seen"] = sorted(stock_seen)[-500:]
    save_state(state)

    print(
        f"[news] {now.strftime('%H:%M')} market_hits={len(market_hits)} "
        f"stock_hits={len(stock_hits)} (polled {len(alerted)} tickers)"
    )
    if not market_hits and not stock_hits:
        return

    lines = [f"📰 뉴스 알림 [{now.strftime('%H:%M KST')}]"]

    if market_hits:
        lines.append("")
        lines.append("— 시장 속보 —")
        for source, item, kw in market_hits:
            lines.append(f"• [{source}/{kw}] {item['title']}")
            if item.get("link"):
                lines.append(f"  {item['link']}")

    if stock_hits:
        lines.append("")
        lines.append("— 단타 후보 종목 —")
        for ticker6, name, title, url, kw, direction in stock_hits[:MAX_STOCK_ALERTS_PER_RUN]:
            icon = "🟢" if direction == "bull" else "🔴"
            label = f"{name} ({ticker6})" if name else ticker6
            lines.append(f"{icon} [{label}/{kw}] {title}")
            lines.append(f"  {url}")

    msg = "\n".join(lines)
    print("--- news alert ---")
    print(msg)
    print("------------------")
    send_telegram(msg)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
