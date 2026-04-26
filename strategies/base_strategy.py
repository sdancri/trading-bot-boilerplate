"""
strategies/base_strategy.py
===============================================================================
CONTRACT pentru orice strategie noua — CITESTE INTEGRAL INAINTE DE A SCRIE COD.

Acest fisier contine:
  1. Clasa `Strategy` (abstract base class) pe care ORICE strategie noua
     TREBUIE sa o mosteneasca.
  2. Dataclass-ul `StrategyContext` — wrapper-ul dat ca prim argument in
     toate hook-urile. Contine state-ul bot-ului + helper-i pt chart/Telegram.
  3. `validate_sl()` — helper pt validarea limitelor SL% (citit din env).
  4. Un TEMPLATE concret la final — copy-paste + modifica pt strategia ta.

REGULI INTANGIBILE (pt AI assistant):
  - NU modifica fisierele din `core/` sau `main.py`. Ele sunt framework-ul.
  - NU modifica acest fisier. El e contractul.
  - Strategia mosteneste `Strategy` si implementeaza `on_start`, `on_candle`.
  - Opțional override `on_trade_closed` si `on_order_event`.
  - TOATE hook-urile sunt `async def` — foloseste `await` pt I/O, niciodata
    `time.sleep` sau functii blocante (folosesc asyncio single-thread).
  - PnL-ul trade-urilor e tras automat de pe Bybit dupa apelul
    `record_closed_trade()` din main — NU calcula PnL local.
===============================================================================
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.bot_state import BotState, TradeRecord


# =============================================================================
# 1. SL LIMITS — validare distanta stop-loss
# =============================================================================

def _sl_min_pct() -> float:
    return float(os.getenv("SL_MIN_PCT", "0.0"))


def _sl_max_pct() -> float:
    return float(os.getenv("SL_MAX_PCT", "100.0"))


def validate_sl(entry_price: float, sl_price: float,
                sl_min: Optional[float] = None,
                sl_max: Optional[float] = None) -> tuple[bool, float, str]:
    """
    Valideaza ca distanta |entry - sl| / entry e in range-ul configurat.

    Returns:
        (ok: bool, sl_pct: float, reason: str)
        - ok=True  -> sl_pct in range, reason=""
        - ok=False -> reason explica de ce (ex: "SL 0.12% < min 0.15%")

    Example:
        ok, pct, reason = validate_sl(80000, 79720)
        if not ok:
            print(f"Skip trade: {reason}")
            return
    """
    lo = sl_min if sl_min is not None else _sl_min_pct()
    hi = sl_max if sl_max is not None else _sl_max_pct()
    if entry_price <= 0:
        return False, 0.0, "entry_price <= 0"
    sl_dist = abs(entry_price - sl_price)
    sl_pct  = sl_dist / entry_price * 100
    if sl_pct < lo:
        return False, sl_pct, f"SL {sl_pct:.3f}% < min {lo}%"
    if sl_pct > hi:
        return False, sl_pct, f"SL {sl_pct:.3f}% > max {hi}%"
    return True, sl_pct, ""


# =============================================================================
# 2. StrategyContext — primit ca `ctx` in toate hook-urile
# =============================================================================

@dataclass
class StrategyContext:
    """
    Context-ul pasat la fiecare hook al strategiei.

    FOLOSIRE in strategie:

        async def on_start(self, ctx: StrategyContext):
            # Access la equity & history
            print(ctx.state.initial_account)          # 100.0
            print(ctx.state.account)                   # curent (= initial + Σ PnL)
            print(len(ctx.state.trades))               # cate trade-uri deja

            # Inregistrare indicatori pt chart (overlay pe price)
            ctx.register_indicator("EMA20", "#ffd700", line_width=2)

            # Trimite notificare Telegram
            await ctx.send_telegram("Ready", "Strategy loaded")

        async def on_candle(self, ctx, candle):
            # Publicare valoare indicator pe chart
            await ctx.push_indicator("EMA20", candle["ts"], my_ema)
    """
    state:                 Any     # core.bot_state.BotState (forward ref)
    symbol:                str     # ex "BTCUSDT"
    bot_name:              str     # din env BOT_NAME
    broadcast:             Callable[[dict], Awaitable[None]]
    send_telegram:         Callable[[str, str], Awaitable[None]]      # (title, body)
    register_indicator:    Callable[[str, str, int, int], None]       # (name, color, lw, ls)
    push_indicator:        Callable[[str, int, float], Awaitable[None]]   # (name, ts_s, value)
    # Poziția activă (linii LIVE Entry/SL/TP pe chart, persistă în /api/init).
    # Semnatura: (direction, entry, sl, tp, qty=None, risk_usd=None) -> None
    # Daca treci qty si risk_usd, chart-ul afiseaza uPnL live ($ + R-multiple).
    set_active_position:   Callable[..., Awaitable[None]]
    clear_active_position: Callable[[], Awaitable[None]]


# =============================================================================
# 3. STRUCTURA `current_candle` — ce primeste on_candle
# =============================================================================
#
# Hook-ul `on_candle` primeste un dict cu urmatoarele campuri:
#
#   candle = {
#       "ts":        int,         # timestamp UTC in SECUNDE (ex 1700000000)
#       "open":      float,       # pretul de deschidere
#       "high":      float,       # maxim intra-bar (in timp real daca !confirmed)
#       "low":       float,       # minim intra-bar (in timp real daca !confirmed)
#       "close":     float,       # close curent (se misca pt unconfirmed)
#       "confirmed": bool,        # True daca bara e inchisa, False daca inca se formeaza
#   }
#
# IMPORTANT (anti-lookahead):
#   - Daca `confirmed=True`  -> bara e INCHISA. Valorile sunt finale.
#     OK pt calcul indicatori si pt decizii de entry.
#   - Daca `confirmed=False` -> bara inca se formeaza. Close-ul se poate schimba.
#     OK pt verificare SL/TP hit cu high/low (piata a trecut deja prin ele).
#     NU OK pt calcul indicatori sau decizii de entry pe close.
#
# Pattern tipic:
#   async def on_candle(self, ctx, candle):
#       # SL/TP check — pe orice tick (confirmed sau nu)
#       if self._in_trade:
#           await self._check_sl_tp(ctx, candle)
#           return
#
#       # Entry logic — DOAR pe bare inchise
#       if not candle["confirmed"]:
#           return
#
#       self._update_indicators(candle["close"])
#       if self._signal():
#           await self._enter(ctx, candle)
#
# =============================================================================


# =============================================================================
# 4. Strategy — ABC pe care o mostenesti
# =============================================================================

class Strategy(ABC):
    """
    Baza pentru orice strategie. Mostenire obligatorie.

    Subclasa trebuie sa:
      - Apeleze super().__init__(name, symbol, interval, history_bars) in __init__
      - Implementeze `on_start(ctx)` si `on_candle(ctx, candle)`
      - Opțional override `on_trade_closed(ctx, trade)` si `on_order_event(...)`

    Dupa load_history(), `self.history` contine bare ISTORICE INCHISE (filtrate
    de lookahead). Strategia poate itera prin ele in on_start pt warmup-ul
    indicatorilor, fara ca aceste valori sa apara pe chart.
    """

    def __init__(self,
                 name:         str,
                 symbol:       str,
                 interval:     str = "5",        # Bybit: 1,3,5,15,30,60,240,D
                 history_bars: int = 300) -> None:
        self.name         = name
        self.symbol       = symbol
        self.interval     = interval
        self.history_bars = history_bars
        self.history:     list[dict] = []          # populat de main.py inainte de on_start

    # -------------------------------------------------------------------------
    # HOOK 1 — on_start (obligatoriu)
    # -------------------------------------------------------------------------
    @abstractmethod
    async def on_start(self, ctx: StrategyContext) -> None:
        """
        Chemat O DATA la pornire, DUPA ce `self.history` e populat cu bare
        istorice inchise (no lookahead).

        Pattern tipic:
          1. ctx.register_indicator(...)  pt fiecare indicator overlay
          2. warmup: for bar in self.history: self._update_indicators(bar["close"])
          3. await ctx.send_telegram(...) — opțional, anunt de pornire
        """
        ...

    # -------------------------------------------------------------------------
    # HOOK 2 — on_candle (obligatoriu)
    # -------------------------------------------------------------------------
    @abstractmethod
    async def on_candle(self, ctx: StrategyContext, candle: dict) -> None:
        """
        Chemat pentru FIECARE update din WS Bybit.

        Args:
          candle: dict cu ts/open/high/low/close/confirmed (vezi sectiunea 3 sus).

        Pattern tipic: check SL/TP pe orice tick, entry doar pe confirmed.
        Vezi sectiunea 3 pentru detalii anti-lookahead.
        """
        ...

    # -------------------------------------------------------------------------
    # HOOK 3 — on_trade_closed (opțional)
    # -------------------------------------------------------------------------
    async def on_trade_closed(self, ctx: StrategyContext,
                              trade: 'TradeRecord') -> None:
        """
        Chemat DUPA ce un trade e inchis si PnL-ul real a fost tras de pe Bybit.

        `trade.pnl` este PnL-ul REAL Bybit (fees incluse).
        Default: no-op. Override pt analiza/log/alerta.
        """
        return None

    # -------------------------------------------------------------------------
    # HOOK 4 — on_resume (opțional)
    # -------------------------------------------------------------------------
    async def on_resume(self, ctx: StrategyContext, last_close: float) -> None:
        """
        Chemat DUPA `on_start` si DUPA ce REST sync s-a ancorat, doar daca
        DATA_DIR e setat si state-ul a fost incarcat din disk.

        Util pentru strategii care au state intra-sesiune (range, anchor,
        leg-uri parțiale) si care vor sa decida dupa restart daca:
          - reia trade-ul activ (verifica pozitia pe Bybit)
          - sare peste sesiunea curenta (pretul deja in afara range-ului)
          - reseteaza state intern de strategie

        Args:
          last_close: ultima inchidere 3m/5m fetch-uita prin REST (UTC s)

        FOOTGUN — daca strategia ta tine un thread.Lock pentru state intern:
        nu apela `_state.save()` sub propriul lock daca pe acel thread se
        produc deja apeluri la save (deadlock daca lock-ul nu e reentrant).

        Default: no-op.
        """
        return None

    # -------------------------------------------------------------------------
    # HOOK 5 — on_order_event (opțional, dar FOARTE recomandat)
    # -------------------------------------------------------------------------
    async def on_order_event(self, ctx: StrategyContext,
                             event_type: str, data: dict) -> None:
        """
        Chemat pentru fiecare eveniment din Bybit Private WS.

        event_type: "order" | "execution" | "position"
        data:       raw dict Bybit event

        Campuri relevante pentru `event_type == "order"`:
          orderId, orderStatus, cumExecQty, leavesQty, avgPrice, rejectReason
          orderStatus ∈ {New, PartiallyFilled, Filled, Cancelled, Rejected,
                         Untriggered, Triggered, Deactivated}

        Override OBLIGATORIU daca:
          - strategia plaseaza orders si prespune ca sunt executate
          - vrei sa gestionezi partial fills (qty real < qty cerut)
          - vrei sa resetezi state-ul la Rejected
        """
        return None

    # -------------------------------------------------------------------------
    # Helper — nu-l override. Chemat de main.py inainte de on_start.
    # -------------------------------------------------------------------------
    async def load_history(self) -> list:
        """
        Fetch `self.history_bars` lumanari INCHISE (anti-lookahead).
        Returneaza list[dict] ASC ordonat: [{ts, open, high, low, close, volume}].

        `ts` este in SECUNDE UTC — identic cu ce primeste on_candle prin WS.
        (Consistenta previne bug-uri in orice logica care face pd.to_datetime(ts).)

        Bara curenta in curs e exclusa automat — prima bara "vie" o vezi in on_candle.
        """
        import time
        import core.exchange_api as ex
        import core.no_lookahead as nl

        raw = await ex.get_kline(self.symbol, self.interval,
                                 limit=self.history_bars + 2)
        bars = []
        for row in reversed(raw):                      # Bybit DESC -> ASC
            bars.append({
                "ts":     int(row[0]),                  # ms temporar (pt filter)
                "open":   float(row[1]),
                "high":   float(row[2]),
                "low":    float(row[3]),
                "close":  float(row[4]),
                "volume": float(row[5]),
            })
        # Elim bara curenta in curs (filter_closed_bars lucreaza in ms)
        now_ms = int(time.time() * 1000)
        bars = nl.filter_closed_bars(bars, self.interval, now_ms=now_ms)
        # Normalizare ms -> s pentru consistenta cu on_candle
        for b in bars:
            b["ts"] = b["ts"] // 1000
        return bars[-self.history_bars:]


# =============================================================================
# 5. Schelet minim pentru o strategie (referință API, nu locație)
# =============================================================================
#
# Clasa ta mosteneste Strategy si implementeaza hook-urile. Iata forma minima:
#
# ---------------------------------------------------------------------------
#   from strategies.base_strategy import Strategy, StrategyContext, validate_sl
#   import core.exchange_api as ex
#
#   class MyStrategy(Strategy):
#
#       def __init__(self, symbol: str) -> None:
#           super().__init__(
#               name="my_strategy_v1",
#               symbol=symbol,
#               interval="5",                    # TF Bybit (1/5/15/60/...)
#               history_bars=300,                # cat istoric pt warmup
#           )
#           self._in_trade = False
#
#       async def on_start(self, ctx: StrategyContext) -> None:
#           ctx.register_indicator("MyInd", "#ffd700", 2, 0)
#           for bar in self.history:
#               self._update_indicators(bar["close"])
#           await ctx.send_telegram(f"READY — {self.name}", "Warmup done")
#
#       async def on_candle(self, ctx, candle: dict) -> None:
#           # SL/TP check (pe orice tick — OK si pe unconfirmed)
#           if self._in_trade:
#               await self._check_sl_tp(ctx, candle)
#               return
#
#           # Entry logic — DOAR pe bare inchise
#           if not candle["confirmed"]:
#               return
#
#           self._update_indicators(candle["close"])
#           await ctx.push_indicator("MyInd", candle["ts"], self._ind_value)
#
#           if self._signal():
#               await self._open(ctx, candle)
#
#       async def on_order_event(self, ctx, event_type, data):
#           if event_type == "order" and data.get("orderStatus") == "Rejected":
#               self._in_trade = False
#               await ctx.send_telegram("Order rejected",
#                                       data.get("rejectReason", "?"))
# ---------------------------------------------------------------------------


# =============================================================================
# 6. NoopStrategy — placeholder pt a porni framework-ul fara logica de trading
# =============================================================================

class NoopStrategy(Strategy):
    """
    Strategie placeholder — nu plaseaza ordine, nu publica indicatori.

    Scop: primul test al infrastructurii dupa deploy (verifici ca WS Bybit
    merge, chart-ul se deschide, Telegram notifica) fara risc de trade-uri
    accidentale.

    La prima bara live, afiseaza un log. Atat.
    """

    def __init__(self, symbol: str) -> None:
        super().__init__(
            name="noop",
            symbol=symbol,
            interval="5",
            history_bars=50,     # minim — doar verificam ca load_history merge
        )
        self._first_seen = False

    async def on_start(self, ctx: StrategyContext) -> None:
        await ctx.send_telegram(
            "NOOP STRATEGY READY",
            "Framework-ul ruleaza, insa nu exista logica de trading."
        )
        print(f"  [{self.name}] history loaded: {len(self.history)} bars "
              f"(NOOP — nu face nimic)")

    async def on_candle(self, ctx: StrategyContext, candle: dict) -> None:
        if candle["confirmed"] and not self._first_seen:
            self._first_seen = True
            print(f"  [{self.name}] primul candle confirmed live: "
                  f"ts={candle['ts']} close={candle['close']}")
