"""Minimal Telegram bot sender (no heavy dependencies)."""

import json
import os
import urllib.request


def send_telegram(text: str, disable_preview: bool = True) -> dict:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": disable_preview}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
    parsed = json.loads(body)
    if not parsed.get("ok"):
        raise RuntimeError(f"Telegram send failed: {body}")
    return parsed
