"""WhatsApp delivery via CallMeBot (free, sends only to your own number).

One-time setup: follow https://www.callmebot.com/blog/free-api-whatsapp-messages/
(add their current WhatsApp number to your contacts, send it
"I allow callmebot to send me messages"); it replies with your API key. Put both values in .env (never commit .env):
    CALLMEBOT_PHONE=+92XXXXXXXXXX
    CALLMEBOT_APIKEY=123456
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

MAX_CHARS = 1500  # keep each WhatsApp message comfortably small


def load_env(path: Path = Path(__file__).resolve().parent.parent / ".env") -> None:
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def chunks(text: str, n: int = MAX_CHARS) -> list[str]:
    out, cur = [], ""
    for line in text.splitlines():
        if len(cur) + len(line) + 1 > n and cur:
            out.append(cur); cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out


def send_whatsapp(text: str, dry_run: bool = False) -> None:
    load_env()
    phone, key = os.environ.get("CALLMEBOT_PHONE"), os.environ.get("CALLMEBOT_APIKEY")
    if dry_run or not (phone and key):
        print(("[dry-run]\n" if dry_run else "[no WhatsApp credentials in .env - printing]\n") + text)
        return
    for part in chunks(text):
        r = requests.get("https://api.callmebot.com/whatsapp.php",
                         params={"phone": phone, "text": part, "apikey": key}, timeout=30)
        r.raise_for_status()
