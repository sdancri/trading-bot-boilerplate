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

import html
import os
import httpx

TELEGRAM_API = "https://api.telegram.org"


def _header() -> str:
    """
    Header standard pt fiecare mesaj. Format:
        🤖 [BOT_NAME · STRATEGY_NAME] SYMBOL    (daca STRATEGY_NAME e setat)
        🤖 [BOT_NAME] SYMBOL                     (altfel)

    Valorile env sunt HTML-escaped — protectie impotriva 400 "can't parse
    entities" daca cineva seteaza un nume cu `<`, `>` sau `&` in valoare.
    """
    name     = html.escape(os.getenv("BOT_NAME", "bot"))
    strategy = html.escape(os.getenv("STRATEGY_NAME", "").strip())
    symbol   = html.escape(os.getenv("SYMBOL",  ""))
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

    `title` e HTML-escaped (e text plain). `body` NU e escape-uit — caller-ul
    e responsabil sa foloseasca tag-uri HTML valide (ex: <code>, <b>) si sa
    nu insereze `<` literal in valori dinamice (foloseste html.escape() daca
    interpolezi text necontrolat).
    """
    safe_title = html.escape(title)
    text = f"{_header()}\n<b>{safe_title}</b>"
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
                print(f"  [TG] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  [TG] error: {e}")
