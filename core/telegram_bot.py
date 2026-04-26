"""
telegram_bot.py — Notificari Telegram cu BOT_NAME prefix
=========================================================
Env vars:
    TELEGRAM_TOKEN   = "123456:ABC..."
    TELEGRAM_CHAT_ID = "123456789"
    BOT_NAME         = "orb_v4"           # apare in header-ul fiecarui mesaj
    SYMBOL           = "BTCUSDT"          # apare in header

Utilizare:
    import core.telegram_bot as tg
    await tg.send("ENTRY LONG", "Entry: 85123.4 | SL: 84952.1")
    await tg.send_raw("Mesaj brut fara header")
"""
from __future__ import annotations

import os
import httpx

TELEGRAM_API = "https://api.telegram.org"


def _header() -> str:
    """
    Header standard pt fiecare mesaj. Format:
        🤖 [BOT_NAME · STRATEGY_NAME] SYMBOL    (daca STRATEGY_NAME e setat)
        🤖 [BOT_NAME] SYMBOL                     (altfel)
    """
    name     = os.getenv("BOT_NAME", "bot")
    strategy = os.getenv("STRATEGY_NAME", "").strip()
    symbol   = os.getenv("SYMBOL",  "")
    label    = f"{name} · {strategy}" if strategy else name
    if symbol:
        return f"🤖 <b>[{label}]</b> <code>{symbol}</code>"
    return f"🤖 <b>[{label}]</b>"


async def send(title: str, body: str = "") -> None:
    """
    Trimite un mesaj cu header standard:
        🤖 [bot_name] SYMBOL
        <b>TITLE</b>
        body

    Daca `body` e gol, `title` apare singur sub header.
    """
    text = f"{_header()}\n<b>{title}</b>"
    if body:
        text += f"\n{body}"
    await send_raw(text)


async def send_raw(text: str) -> None:
    """Trimite text brut (pt cazuri speciale). Respecta HTML parse mode."""
    token   = os.getenv("TELEGRAM_TOKEN",   "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print(f"  [TG] not configured — {text[:100]}")
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{TELEGRAM_API}/bot{token}/sendMessage", json={
                "chat_id":                  chat_id,
                "text":                     text,
                "parse_mode":               "HTML",
                "disable_web_page_preview": True,
            })
            if r.status_code != 200:
                print(f"  [TG] HTTP {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"  [TG] error: {e}")
