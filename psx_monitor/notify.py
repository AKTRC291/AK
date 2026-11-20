"""Alert delivery: CallMeBot WhatsApp (free, own number only) or Telegram bot (free, official API).

Pick the channel with ALERT_PROVIDER in .env: "callmebot" (default) or "telegram".

Telegram setup (about 3 minutes):
  1. In Telegram, message @BotFather -> /newbot -> pick a name -> copy the token.
  2. Open your new bot and press Start (send it any message).
  3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates in a browser;
     copy the number at "chat":{"id": ... }.
  .env:  ALERT_PROVIDER=telegram
         TELEGRAM_BOT_TOKEN=123456:ABC...
         TELEGRAM_CHAT_ID=123456789

CallMeBot WhatsApp:

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


def _send_telegram(text: str, token: str, chat: str) -> None:
    for part in chunks(text, 3800):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": part.replace("*", "")}, timeout=30)
        r.raise_for_status()


ALERTS = Path(__file__).resolve().parent.parent / "alerts"


def _write_alert(text: str, job: str) -> None:
    """Save the alert for the Claude app relay (scripts/run.cmd pushes alerts/ to GitHub)."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Asia/Karachi"))
    ALERTS.mkdir(exist_ok=True)
    body = f"generated: {now:%Y-%m-%d %H:%M} PKT\n{text.strip()}\n"
    (ALERTS / f"latest_{job}.txt").write_text(body, encoding="utf-8")
    if job == "intraday":   # keep the day's intraday alerts together
        with open(ALERTS / f"intraday_{now:%Y-%m-%d}.txt", "a", encoding="utf-8") as f:
            f.write(body + "\n")
    print(f"[claude] alert saved to alerts/latest_{job}.txt")


def send_whatsapp(text: str, dry_run: bool = False, job: str = "misc") -> None:
    """Send an alert on the configured channel (name kept for compatibility).

    ALERT_PROVIDER: "claude" (default; saved to alerts/ and relayed to the Claude app),
    "telegram", or "callmebot".
    """
    load_env()
    provider = os.environ.get("ALERT_PROVIDER", "claude").strip().lower()
    if dry_run:
        print("[dry-run]\n" + text); return
    if provider == "claude":
        return _write_alert(text, job)
    if provider == "telegram":
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if token and chat:
            return _send_telegram(text, token, chat)
        print("[no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env - printing]\n" + text); return
    phone, key = os.environ.get("CALLMEBOT_PHONE"), os.environ.get("CALLMEBOT_APIKEY")
    if not (phone and key):
        print("[no WhatsApp credentials in .env - printing]\n" + text)
        return
    for part in chunks(text):
        r = requests.get("https://api.callmebot.com/whatsapp.php",
                         params={"phone": phone, "text": part, "apikey": key}, timeout=30)
        r.raise_for_status()
