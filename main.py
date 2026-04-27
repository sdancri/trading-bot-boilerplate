"""
main.py — Framework principal trading bot
===========================================
Ce face:
  1. La startup:
     - Importa strategia din STRATEGY_MODULE (env) si instantiaza
     - Fetch istoric N lumanari de pe Bybit (pentru indicatori) — NU se
       trimit la chart, ramane doar in strategy state
     - Ruleaza strategy.on_start(ctx)
     - Porneste WS Bybit (live candles)
     - Porneste HTTP server pe CHART_PORT (default 8090)

  2. La fiecare candle din WS:
     - Pastreaza ts-ul primei lumanari ca state.first_candle_ts
     - Append la _candles (afisate pe chart)
     - Broadcast la toti clientii WS
     - Apeleaza strategy.on_candle(ctx, ...)

  3. Cand strategia inchide un trade:
     - Strategia apeleaza record_closed_trade(entry_ts, exit_ts, ...)
     - Framework-ul trage PnL-ul REAL de pe Bybit (fetch_pnl_for_trade)
     - Adauga TradeRecord in BotState
     - Broadcast trade + equity catre clienti
     - Trimite Telegram
     - Cheama strategy.on_trade_closed(ctx, trade)

Env vars importante:
  BOT_NAME             — apare in Telegram si pe chart
  SYMBOL               — "BTCUSDT"
  STRATEGY_MODULE      — "strategies.base_strategy" (modulul Python)
  STRATEGY_CLASS       — "NoopStrategy" (numele clasei din modul)
  CHART_PORT           — 8090 (port diferit fata de vechile boturi pe 8080)
  CHART_TZ             — "Europe/Bucharest"
  ACCOUNT_SIZE         — 100.0 (plecarea pt state.account)
  SL_MIN_PCT           — 0.0 (filtru minim SL%)
  SL_MAX_PCT           — 100.0 (filtru maxim SL%)
"""
from __future__ import annotations

import asyncio
import importlib
import io as _io
import json
import os
import sys as _sys
import time as time_mod
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# UTF-8 stdout pe Windows/Docker + line-buffering pt logs live in `docker logs -f`.
# Fara line_buffering=True, TextIOWrapper face buffering per-block (~8KB) si
# `PYTHONUNBUFFERED=1` din Dockerfile e override-uit de acest wrapper.
_sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8",
                                errors="replace", line_buffering=True)
_sys.stderr = _io.TextIOWrapper(_sys.stderr.buffer, encoding="utf-8",
                                errors="replace", line_buffering=True)

import core.exchange_api as ex
import core.telegram_bot as tg
from core.bot_state import BotState, TradeRecord
from strategies.base_strategy import Strategy, StrategyContext


# ============================================================================
# Last-resort crash handler — captureaza exceptii Python necapturate
# (NU prinde SIGKILL, OOM kill, segfault — pt ăstea trebuie watchdog extern)
# ============================================================================

def _crash_excepthook(exc_type, exc_value, tb):
    import traceback
    err_text = ''.join(traceback.format_exception(exc_type, exc_value, tb))
    # log local intai (chiar daca TG eseueaza)
    print(f"[CRASH] uncaught {exc_type.__name__}: {exc_value}", file=_sys.stderr)
    print(err_text, file=_sys.stderr)
    # Trimit TG sincron via httpx, evit asyncio (event loop poate fi deja mort)
    try:
        import httpx as _httpx, html as _html
        token = os.getenv("TELEGRAM_TOKEN", "")
        chat = os.getenv("TELEGRAM_CHAT_ID", "")
        if token and chat:
            name = _html.escape(os.getenv("BOT_NAME", "bot"))
            sym  = _html.escape(os.getenv("SYMBOL", ""))
            head = f"🤖 <b>[{name}]</b> <code>{sym}</code>" if sym else f"🤖 <b>[{name}]</b>"
            tb_short = _html.escape(err_text[-1500:])  # ultima parte din stack
            text = f"{head}\n<b>BOT CRASHED 💥</b>\n<code>{exc_type.__name__}: {_html.escape(str(exc_value))[:200]}</code>\n<pre>{tb_short}</pre>"
            _httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
                        timeout=5)
    except Exception:
        pass
    # respect default behavior — print + exit nonzero
    _sys.__excepthook__(exc_type, exc_value, tb)


_sys.excepthook = _crash_excepthook


# ============================================================================
# Config
# ============================================================================

BOT_NAME        = os.getenv("BOT_NAME", "bot")
SYMBOL          = os.getenv("SYMBOL", "BTCUSDT")
STRATEGY_MODULE = os.getenv("STRATEGY_MODULE", "strategies.base_strategy")
STRATEGY_CLASS  = os.getenv("STRATEGY_CLASS",  "NoopStrategy")
CHART_PORT      = int(os.getenv("CHART_PORT", "8090"))
CHART_TZ        = os.getenv("CHART_TZ", "Europe/Bucharest")
WS_INTERVAL     = os.getenv("WS_KLINE_INTERVAL", "5")    # 1/3/5/15/30/60/...

BYBIT_WS = "wss://stream.bybit.com/v5/public/linear"

BASE   = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")


# ============================================================================
# Shared state
# ============================================================================

_state:    BotState  = BotState()                 # account + trades
_candles:  list[list] = []                         # [[ts_s, o, h, l, c], ...]
_clients:  set[WebSocket] = set()
_strategy: Optional[Strategy] = None
_ctx:      Optional[StrategyContext] = None

# Pozitia activa curenta — broadcast pe chart ca linii LIVE Entry/SL/TP +
# inclus in /api/init pt persistenta la refresh. Strategia o intretine prin
# ctx.set_active_position() / ctx.clear_active_position().
_active_position: Optional[dict] = None   # {"direction", "entry", "sl", "tp"} sau None

# ---- Sincronizare REST <-> WS ---------------------------------------------
# Asigura ca la (re)conectare WS:
#   1. Nu procesam de 2 ori aceeasi bara confirmed (anti-duplicat)
#   2. Nu pierdem bare intre ultima bara din istoric si primul tick WS
#      (anti-gap). Daca detectam gap -> fetch REST si procesam barele lipsa.
_last_synced_ts: Optional[int] = None   # secunde UTC — ultima bara confirmed procesata
_sync_done:      bool = False           # True dupa primul gap-check al fiecarei sesiuni WS


# ============================================================================
# Broadcast helpers
# ============================================================================

async def _broadcast(payload: dict) -> None:
    if not _clients:
        return
    msg = json.dumps(payload)
    dead: set[WebSocket] = set()
    for ws in _clients.copy():
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    _clients.difference_update(dead)


# ============================================================================
# Indicatori — strategy publica, chart afiseaza de la first_candle_ts
# ============================================================================

def register_indicator(name: str, color: str = "#ffd700",
                       line_width: int = 1, line_style: int = 0) -> None:
    """
    Apelata de obicei in strategy.on_start() — inregistreaza stilul
    indicatorului care va fi afisat pe chart.
    """
    _state.register_indicator(name, color, line_width, line_style)


async def push_indicator(name: str, ts_s: int, value: float) -> None:
    """
    Publica un punct al unui indicator pe chart.
    Strategia calculeaza intern (warmup pe istoric) si cheama asta DOAR
    pt valorile vizibile pe chart (de la prima bara live incoace).
    """
    _state.add_indicator_point(name, ts_s, value)
    await _broadcast({
        "type":  "indicator",
        "name":  name,
        "time":  int(ts_s),
        "value": round(float(value), 6),
    })


# ============================================================================
# Active position — linii LIVE Entry/SL/TP pe chart (persista la refresh via /api/init)
# ============================================================================

async def set_active_position(direction: str, entry: float,
                              sl: float, tp: float,
                              qty: Optional[float] = None,
                              risk_usd: Optional[float] = None) -> None:
    """
    Apelata de strategie la _open() — memoreaza pozitia deschisa si o
    broadcast-eaza la toti clientii (chart deseneaza liniile LIVE).

    qty si risk_usd sunt OPTIONALE — daca strategia le furnizeaza,
    chart-ul afiseaza uPnL live ($ + R-multiple) in timpul trade-ului.
    """
    global _active_position
    _active_position = {
        "direction": direction,
        "entry":     float(entry),
        "sl":        float(sl),
        "tp":        float(tp),
    }
    if qty is not None:
        _active_position["qty"] = float(qty)
    if risk_usd is not None:
        _active_position["risk_usd"] = float(risk_usd)
    await _broadcast({"type": "position_open", **_active_position})


async def clear_active_position() -> None:
    """
    Apelata de strategie la close (TP/SL/REV/REJECT) — uita pozitia curenta
    si anunta clientii sa stearga liniile LIVE.
    """
    global _active_position
    _active_position = None
    await _broadcast({"type": "position_close"})


# ============================================================================
# Trade recording — trage PnL de pe Bybit, apoi broadcast + Telegram
# ============================================================================

async def record_closed_trade(direction:   str,
                              entry_ts_ms: int,
                              entry_price: float,
                              sl_price:    float,
                              tp_price:    Optional[float],
                              qty:         float,
                              exit_ts_ms:  int,
                              exit_price:  float,
                              exit_reason: str,
                              extra:       Optional[dict] = None) -> TradeRecord:
    """
    Helper pe care il apeleaza strategia cand un trade se inchide.

    CONTRACT:
      De pe Bybit se trage DOAR valoarea PnL realizat (via /v5/position/closed-pnl).
      Balance-ul / equity-ul NU este interogat niciodata.
      Equity-ul local din BotState.account se actualizeaza prin:
          state.account += pnl_bybit
      (vezi BotState.add_closed_trade)

    Workflow:
      1. Asteapta 2s (Bybit inregistreaza closed-pnl cu mica latenta)
      2. fetch_pnl_for_trade() -> PnL real (principal + piramide + fees)
      3. Construieste TradeRecord cu PnL-ul real
      4. BotState.add_closed_trade() — equity se recalculeaza LOCAL
      5. Broadcast la toti clientii (refresh panel trades + equity curve)
      6. Trimite Telegram cu numele botului + strategiei
      7. Cheama strategy.on_trade_closed()
    """
    # --- PAS 1+2: trage DOAR PnL-ul, nu balance-ul ---
    pnl_data = await ex.fetch_pnl_for_trade(SYMBOL, entry_ts_ms, exit_ts_ms)

    # --- PAS 3: construieste TradeRecord ---
    trade = TradeRecord(
        id=0,                           # setat de add_closed_trade()
        date=datetime.fromtimestamp(entry_ts_ms / 1000, tz=timezone.utc)
                      .strftime("%Y-%m-%d"),
        direction=direction,
        entry_ts=entry_ts_ms,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_price=tp_price,
        qty=qty,
        exit_ts=exit_ts_ms,
        exit_price=exit_price,
        exit_reason=exit_reason,
        pnl=pnl_data["pnl"],            # PnL REAL Bybit (USD net dupa fees)
        fees=pnl_data["fees"],
        extra=extra or {},
    )

    # --- PAS 4: equity se calculeaza LOCAL (state.account += trade.pnl) ---
    _state.add_closed_trade(trade)

    # Persista state-ul (no-op daca DATA_DIR nu e setat).
    # Rulam in thread pool ca sa nu blocam event loop-ul cu file I/O.
    await asyncio.to_thread(_state.save)

    # Broadcast trade nou + equity actualizat
    await _broadcast({
        "type":    "trade_closed",
        "trade":   trade.to_dict(),
        "equity":  _state.equity_curve[-1],
        "summary": _state.summary(),
    })

    # Telegram
    sign = "📈" if trade.pnl >= 0 else "📉"
    strat_name = _strategy.name if _strategy else "?"
    await tg.send(
        f"{sign} TRADE INCHIS — {direction}",
        f"<b>Strategy:</b> <code>{strat_name}</code>\n"
        f"Exit: {exit_price:,.{int(os.getenv('PRICE_PRECISION', '2'))}f}  ({exit_reason})\n"
        f"PnL: <b>${trade.pnl:+,.2f}</b>  (Bybit real, fees incluse)\n"
        f"Account: ${_state.account:,.2f}  |  Return: "
        f"{(_state.account - _state.initial_account) / _state.initial_account * 100:+.2f}%"
    )

    # Strategy hook (daca vrea sa faca ceva dupa)
    if _strategy and _ctx:
        try:
            await _strategy.on_trade_closed(_ctx, trade)
        except Exception as e:
            print(f"  [STRATEGY] on_trade_closed error: {e}")

    return trade


# ============================================================================
# Bootstrap — load strategy, fetch history, run on_start
# ============================================================================

async def _bootstrap() -> None:
    global _strategy, _ctx

    print(f"\n{'=' * 60}")
    print(f"  {BOT_NAME.upper()} starting")
    print(f"  Symbol:   {SYMBOL}")
    print(f"  Strategy: {STRATEGY_MODULE}.{STRATEGY_CLASS}")
    print(f"  Chart:    http://0.0.0.0:{CHART_PORT}/  (TZ: {CHART_TZ})")
    print(f"  Account:  ${_state.initial_account:,.2f}")
    print(f"{'=' * 60}\n")

    # 0. Restore state din disk (no-op daca DATA_DIR nu e setat / fisier lipsa).
    # Daca env RESET_TOKEN difera de cel stocat -> wipe.
    await asyncio.to_thread(_state.load)

    # 1. Import + instantiate strategy
    mod = importlib.import_module(STRATEGY_MODULE)
    cls = getattr(mod, STRATEGY_CLASS)
    _strategy = cls(symbol=SYMBOL)
    print(f"  [BOOT] Strategy '{_strategy.name}' loaded")

    # 2. Build context
    _ctx = StrategyContext(
        state=_state,
        symbol=SYMBOL,
        bot_name=BOT_NAME,
        broadcast=_broadcast,
        send_telegram=tg.send,
        register_indicator=register_indicator,
        push_indicator=push_indicator,
        set_active_position=set_active_position,
        clear_active_position=clear_active_position,
    )

    # 3. Load historical candles — NU apar pe chart, doar pt indicatori
    try:
        history = await _strategy.load_history()
        print(f"  [BOOT] Loaded {len(history):,} istoric candles "
              f"(pt indicatori — NU apar pe chart)")
        _strategy.history = history
    except Exception as e:
        print(f"  [BOOT] load_history FAILED: {e}")
        _strategy.history = []

    # 3b. Ancoreaza sync-ul REST<->WS (independent de load_history)
    await _sync_anchor_rest()

    # 4. Run on_start
    try:
        await _strategy.on_start(_ctx)
    except Exception as e:
        import traceback
        print(f"  [STRATEGY] on_start CRASHED:\n{traceback.format_exc()}")

    # 4b. Daca state-ul a fost incarcat din disk si avem o referinta la
    # ultima bara, dam strategiei sansa sa decida dupa restart (ex: skip
    # sesiunea curenta daca pretul e deja in afara range-ului).
    last_close = _strategy.history[-1]["close"] if _strategy.history else None
    if last_close is not None:
        try:
            await _strategy.on_resume(_ctx, float(last_close))
        except Exception:
            import traceback
            print(f"  [STRATEGY] on_resume CRASHED:\n{traceback.format_exc()}")

    # 5. Notify Telegram
    await tg.send(
        "BOT STARTED ✅",
        f"Strategy: <code>{_strategy.name}</code>\n"
        f"Account init: ${_state.initial_account:,.2f}\n"
        f"Chart: port {CHART_PORT}"
    )


# ============================================================================
# Bybit Private WS handlers (order / execution / position)
# ============================================================================

async def _on_order_event(event: dict) -> None:
    """Log concis + forward la strategy."""
    status = event.get("orderStatus", "?")
    side   = event.get("side", "?")
    qty    = event.get("qty", "?")
    filled = event.get("cumExecQty", "0")
    px     = event.get("avgPrice") or event.get("price", "?")
    reject = event.get("rejectReason", "")

    print(f"  [ORDER] {status} {side} qty={qty} filled={filled} px={px}"
          f"{'  REJECT=' + reject if reject and reject != 'EC_NoError' else ''}")

    if _strategy and _ctx:
        try:
            await _strategy.on_order_event(_ctx, "order", event)
        except Exception as e:
            import traceback
            print(f"  [STRATEGY] on_order_event order crashed:\n{traceback.format_exc()}")


async def _on_execution_event(event: dict) -> None:
    """Fiecare fill individual (partial sau total)."""
    side  = event.get("side", "?")
    qty   = event.get("execQty", "?")
    px    = event.get("execPrice", "?")
    fee   = event.get("execFee", "?")
    etype = event.get("execType", "?")

    print(f"  [EXEC ] {side} {qty} @ {px}  fee={fee}  type={etype}")

    if _strategy and _ctx:
        try:
            await _strategy.on_order_event(_ctx, "execution", event)
        except Exception as e:
            import traceback
            print(f"  [STRATEGY] on_order_event exec crashed:\n{traceback.format_exc()}")


async def _on_position_event(event: dict) -> None:
    """Update pozitie (size, avgPrice, unrealized PnL)."""
    side = event.get("side", "?")
    size = event.get("size", "?")
    avg  = event.get("avgPrice", "?")
    upnl = event.get("unrealisedPnl", "?")

    # Log doar daca e semnificativ (nu la fiecare tick)
    if float(size or 0) > 0:
        print(f"  [POS  ] {side} size={size} avgPx={avg} uPnL={upnl}")

    if _strategy and _ctx:
        try:
            await _strategy.on_order_event(_ctx, "position", event)
        except Exception as e:
            import traceback
            print(f"  [STRATEGY] on_order_event position crashed:\n{traceback.format_exc()}")


# ============================================================================
# Bybit WebSocket consumer
# ============================================================================

async def _sync_anchor_rest() -> None:
    """
    Ancoreaza `_last_synced_ts` cu ts-ul ultimei bare INCHISE disponibile
    pe Bybit. Chemata in _bootstrap() dupa load_history() al strategiei.

    Independent de ce face strategia — serverul are propriul anchor pt a
    detecta ulterior gap-uri intre REST si WS.
    """
    global _last_synced_ts

    import time
    import core.no_lookahead as nl

    try:
        raw = await ex.get_kline(SYMBOL, WS_INTERVAL, limit=5)
        if not raw:
            print("  [SYNC] WARN: n-am putut ancora — get_kline gol")
            return
        # Bybit DESC -> reverse la ASC
        bars = [{"ts": int(r[0]) // 1000} for r in reversed(raw)]
        now_ms = int(time.time() * 1000)
        closed = nl.filter_closed_bars(bars, WS_INTERVAL, now_ms=now_ms, ts_key="ts")
        if closed:
            _last_synced_ts = closed[-1]["ts"]
            dt = datetime.fromtimestamp(_last_synced_ts, tz=timezone.utc)
            print(f"  [SYNC] Anchor: last_synced_ts = {_last_synced_ts}  ({dt})")
        else:
            print("  [SYNC] WARN: niciun closed bar in anchor fetch")
    except Exception as e:
        print(f"  [SYNC] Anchor failed: {e}")


async def _fetch_gap_bars(after_ts_s: int, before_ts_s: int) -> list[dict]:
    """
    Fetch bare confirmed cu ts strict in (after_ts_s, before_ts_s) — capete
    excluse (`after` e deja procesat, `before` va veni prin WS).

    Returneaza bare ASC ordonate.
    """
    import core.no_lookahead as nl

    interval_s = nl.interval_ms(WS_INTERVAL) // 1000
    # +/- o marja de 1 bar pt siguranta la margini
    start_ms = (after_ts_s + 1) * 1000
    end_ms   = before_ts_s * 1000

    raw = await ex.get_kline(SYMBOL, WS_INTERVAL,
                             start=start_ms, end=end_ms, limit=1000)
    bars = []
    for row in reversed(raw):                       # Bybit DESC -> ASC
        ts = int(row[0]) // 1000
        if ts <= after_ts_s or ts >= before_ts_s:
            continue
        bars.append({
            "ts": ts,
            "o":  float(row[1]),
            "h":  float(row[2]),
            "l":  float(row[3]),
            "c":  float(row[4]),
        })
    return bars


async def _process_bar(ts_s: int, o: float, h: float, l: float, c: float,
                       confirmed: bool) -> None:
    """
    Procesare unica per bara (sursa: WS tick SAU gap-fill REST).
    - mark first candle
    - append la _candles (daca confirmed)
    - broadcast la clienti
    - chema strategy.on_candle()
    """
    _state.mark_first_candle(ts_s)

    prec = int(os.getenv("PRICE_PRECISION", "2"))
    if confirmed:
        _candles.append([ts_s,
                         round(o, prec), round(h, prec),
                         round(l, prec), round(c, prec)])
        if len(_candles) > 20000:
            _candles.pop(0)

    await _broadcast({
        "type":      "candle",
        "confirmed": confirmed,
        "data": {"time": ts_s, "open": o, "high": h, "low": l, "close": c},
    })

    if _strategy and _ctx:
        try:
            candle_dict = {
                "ts":        ts_s,
                "open":      o,
                "high":      h,
                "low":       l,
                "close":     c,
                "confirmed": confirmed,
            }
            await _strategy.on_candle(_ctx, candle_dict)
        except Exception as e:
            import traceback
            print(f"  [STRATEGY] on_candle CRASHED:\n{traceback.format_exc()}")


async def _bybit_ws_task() -> None:
    global _sync_done
    topic = f"kline.{WS_INTERVAL}.{SYMBOL}"
    while True:
        try:
            async with websockets.connect(BYBIT_WS,
                                          ping_interval=None,
                                          open_timeout=15) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": [topic]}))
                print(f"  [WS] connected — subscribed {topic}")
                _sync_done = False   # Reset gap-check la fiecare (re)conectare

                async def _hb() -> None:
                    while True:
                        await asyncio.sleep(20)
                        try:
                            await ws.send(json.dumps({"op": "ping"}))
                        except Exception:
                            break

                hb = asyncio.create_task(_hb())
                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("op") in ("pong", "subscribe"):
                            continue
                        if msg.get("topic") != topic:
                            continue
                        for k in msg["data"]:
                            await _handle_candle(k)
                finally:
                    hb.cancel()
        except Exception as e:
            print(f"  [WS] error: {e!r} — reconnect in 5s")
            await asyncio.sleep(5)


async def _handle_candle(k: dict) -> None:
    """
    Proceseaza un tick kline din WS Bybit — cu anti-duplicat si gap-fill.
    """
    global _last_synced_ts, _sync_done

    ts_s      = int(k["start"]) // 1000
    o         = float(k["open"])
    h         = float(k["high"])
    l         = float(k["low"])
    c         = float(k["close"])
    confirmed = bool(k["confirm"])

    import core.no_lookahead as nl
    interval_s = nl.interval_ms(WS_INTERVAL) // 1000

    # ──────────────────────────────────────────────────────────────
    # 1. Anti-duplicat (confirmed bar procesat deja)
    # ──────────────────────────────────────────────────────────────
    if confirmed and _last_synced_ts is not None and ts_s <= _last_synced_ts:
        print(f"  [SYNC] Skip duplicate confirmed {ts_s} "
              f"(last_synced_ts={_last_synced_ts})")
        return

    # ──────────────────────────────────────────────────────────────
    # 2. Gap-fill la primul tick al acestei sesiuni WS
    # ──────────────────────────────────────────────────────────────
    if not _sync_done:
        _sync_done = True
        if _last_synced_ts is not None:
            next_expected = _last_synced_ts + interval_s
            if ts_s > next_expected:
                print(f"  [SYNC] GAP: have {_last_synced_ts}, first WS bar {ts_s}  "
                      f"(expected {next_expected}). Fetching REST...")
                gap = await _fetch_gap_bars(_last_synced_ts, ts_s)
                for b in gap:
                    await _process_bar(b["ts"], b["o"], b["h"], b["l"], b["c"],
                                       confirmed=True)
                    _last_synced_ts = b["ts"]
                print(f"  [SYNC] Gap filled: {len(gap)} bare REST recuperate "
                      f"(last_synced_ts -> {_last_synced_ts})")

    # ──────────────────────────────────────────────────────────────
    # 3. Procesare normala
    # ──────────────────────────────────────────────────────────────
    await _process_bar(ts_s, o, h, l, c, confirmed)

    if confirmed:
        _last_synced_ts = ts_s


# ============================================================================
# FastAPI app
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    await _bootstrap()
    # Public stream — kline (price feed)
    asyncio.create_task(_bybit_ws_task())
    # Private stream — order/execution/position events
    import core.private_ws as pws
    asyncio.create_task(pws.run(
        on_order=_on_order_event,
        on_execution=_on_execution_event,
        on_position=_on_position_event,
    ))
    try:
        yield
    finally:
        # Shutdown notification
        try:
            ret_pct = (_state.account - _state.initial_account) / _state.initial_account * 100
            n_trades = len(_state.equity_curve) - 1 if _state.equity_curve else 0
            await tg.send(
                "BOT STOPPED 🛑",
                f"Strategy: <code>{_strategy.name if _strategy else '?'}</code>\n"
                f"Account: ${_state.account:,.2f}  |  Return: {ret_pct:+.2f}%\n"
                f"Trades: {n_trades}"
            )
        except Exception as e:
            print(f"  [SHUTDOWN] tg.send failed: {e}")


app = FastAPI(lifespan=lifespan, title=f"{BOT_NAME} chart")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def root():
    return FileResponse(os.path.join(STATIC, "chart_live.html"))


def _tf_label(interval: str) -> str:
    """Bybit kline interval ('1','5','60','240','D','W','M') → friendly ('1m','5m','1h','4h','1d','1w','1M')."""
    mapping = {"D": "1d", "W": "1w", "M": "1M"}
    if interval in mapping:
        return mapping[interval]
    try:
        n = int(interval)
    except ValueError:
        return interval
    if n < 60:
        return f"{n}m"
    if n % 60 == 0:
        return f"{n // 60}h"
    return f"{n}m"


@app.get("/api/init")
async def api_init():
    """Payload pentru chart la load."""
    return JSONResponse({
        "candles":         _candles,        # doar cele de la prima pornire
        "active_position": _active_position, # None daca nu e pozitie deschisa
        "timeframe":       _tf_label(WS_INTERVAL),
        **_state.init_payload(),
    })


@app.get("/api/status")
async def api_status():
    last = _candles[-1] if _candles else None
    return {
        "bot_name":           BOT_NAME,
        "symbol":             SYMBOL,
        "candles_total":      len(_candles),
        "last_candle_ts":     last[0] if last else None,
        "connected_clients":  len(_clients),
        "summary":            _state.summary(),
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    try:
        while True:
            await ws.receive_text()   # keep-alive
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)


# ============================================================================
# Entry point
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app",
                host="0.0.0.0",
                port=CHART_PORT,
                reload=False,
                log_level="info")
