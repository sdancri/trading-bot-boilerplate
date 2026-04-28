"""
strategies/example_strategy.py — Strategie exemplu (EMA crossover)
====================================================================
Pur pentru demonstrarea boilerplate-ului. NU e o strategie profitabila.

Logica:
  - Calculeaza EMA fast (9) si EMA slow (21) pe candle-urile 5m
  - Golden cross (fast > slow) → LONG; death cross → SHORT
  - SL = 0.3% sub entry pentru LONG (peste entry pt SHORT)
  - TP = 0.6% (R:R = 1:2)
  - Foloseste filtre SL_MIN_PCT / SL_MAX_PCT (din env)
  - Risk = 5% din ACCOUNT_SIZE initial (no compound)

Ce demonstreaza:
  - load_history() pentru calculul EMA pe date istorice
  - validate_sl() pentru SL limits
  - calc_qty_by_risk() cu account-ul INITIAL (no compound)
  - record_closed_trade() care trage PnL real de pe Bybit
  - Telegram cu BOT_NAME
"""
from __future__ import annotations

import os
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import core.exchange_api as ex
from core.bot_state import ReconciliationError
from strategies.base_strategy import Strategy, StrategyContext, validate_sl


# --- Config strategie ---
EMA_FAST    = 9
EMA_SLOW    = 21
RISK_FRAC   = 0.05      # 5% din ACCOUNT_SIZE initial
SL_PCT      = 0.3       # SL la 0.3% fata de entry
TP_PCT      = 0.6       # TP la 0.6% (R:R = 1:2)


class ExampleStrategy(Strategy):

    def __init__(self, symbol: str) -> None:
        super().__init__(
            name="ema_cross_example",
            symbol=symbol,
            interval="5",
            history_bars=300,    # destul pt EMA_SLOW
        )
        self._closes:   deque[float] = deque(maxlen=max(EMA_FAST, EMA_SLOW) * 4)
        self._ema_fast: Optional[float] = None
        self._ema_slow: Optional[float] = None
        self._last_fast_gt_slow: Optional[bool] = None

        # Trade state — O pozitie la un moment dat
        self._in_trade:     bool = False
        self._dir:          Optional[str] = None
        self._entry_ts:     int = 0
        self._entry_price:  float = 0.0
        self._sl_price:     float = 0.0
        self._tp_price:     float = 0.0
        self._qty:          float = 0.0

        # Halt per-instanta. Setat de _check_sl_tp daca reconcilierea esueaza
        # (qty real > qty local pe Bybit). Strategia ramane "moarta" pe acest
        # simbol pana la restart manual — nu se inchide poziția existenta.
        self._halted:       bool = False

    # ------------------------------------------------------------------
    # on_start — WARMUP EMA pe istoric (intern, fara publicare pe chart)
    # ------------------------------------------------------------------
    async def on_start(self, ctx: StrategyContext) -> None:
        # Inregistreaza stilul indicatorilor pe chart
        ctx.register_indicator("EMA9",  "#00e676", 2, 0)   # verde, solid
        ctx.register_indicator("EMA21", "#ff3352", 2, 0)   # rosu,  solid

        history = getattr(self, "history", [])
        if len(history) < EMA_SLOW + 5:
            print(f"  [{self.name}] NU am destule lumanari pt EMA ({len(history)})")
            return

        # WARMUP — calculeaza EMA pe TOATE barele istorice, intern.
        # Nu apelam push_indicator aici — aceste valori corespund barelor
        # care NU sunt afisate pe chart (chart-ul incepe de la prima bara live).
        for bar in history:
            self._closes.append(bar["close"])
            self._update_emas(bar["close"])

        if self._ema_fast and self._ema_slow:
            self._last_fast_gt_slow = self._ema_fast > self._ema_slow
            print(f"  [{self.name}] WARMUP complet: "
                  f"EMA9={self._ema_fast:.2f}  EMA21={self._ema_slow:.2f}  "
                  f"trend={'UP' if self._last_fast_gt_slow else 'DOWN'}")
            print(f"  [{self.name}] EMA-urile sunt MATURE — publicare pe chart "
                  f"incepe cu prima bara live")

        # Telegram init
        await ctx.send_telegram(
            f"STRATEGY READY — {self.name}",
            f"EMA9 warmed = {self._ema_fast:.2f}\n"
            f"EMA21 warmed = {self._ema_slow:.2f}\n"
            f"SL limits: {os.getenv('SL_MIN_PCT', '0.0')}% / "
            f"{os.getenv('SL_MAX_PCT', '100.0')}%"
        )

    # ------------------------------------------------------------------
    # on_candle — rulata pt fiecare update WS
    # ------------------------------------------------------------------
    async def on_candle(self, ctx: StrategyContext, candle: dict) -> None:
        # Halt: stare divergenta detectata anterior la reconciliere. Nu mai
        # facem nimic pe acest simbol pana la restart manual.
        if self._halted:
            return

        ts        = candle["ts"]
        o         = candle["open"]
        h         = candle["high"]
        l         = candle["low"]
        c         = candle["close"]
        confirmed = candle["confirmed"]

        # Daca suntem in trade, verifica SL/TP pe LIVE (nu doar la confirmed)
        if self._in_trade:
            await self._check_sl_tp(ctx, ts, h, l, c)
            return

        # Entry logic — DOAR pe confirmed candles (evita flip-flop)
        if not confirmed:
            return

        self._closes.append(c)
        self._update_emas(c)
        if not self._ema_fast or not self._ema_slow:
            return

        # Publica valorile EMA pe chart — aceste valori sunt DEJA MATURE
        # (au beneficiat de warmup pe ~300 bare istorice in on_start).
        # Prima bara live vede EMA la valoarea reala, nu la 0.
        await ctx.push_indicator("EMA9",  ts, self._ema_fast)
        await ctx.push_indicator("EMA21", ts, self._ema_slow)

        fast_gt_slow = self._ema_fast > self._ema_slow
        if self._last_fast_gt_slow is None:
            self._last_fast_gt_slow = fast_gt_slow
            return

        # Detect crossover
        direction: Optional[str] = None
        if fast_gt_slow and not self._last_fast_gt_slow:
            direction = "LONG"
        elif not fast_gt_slow and self._last_fast_gt_slow:
            direction = "SHORT"
        self._last_fast_gt_slow = fast_gt_slow

        if direction is None:
            return

        # Setup trade
        entry = c
        if direction == "LONG":
            sl = entry * (1 - SL_PCT / 100)
            tp = entry * (1 + TP_PCT / 100)
            side = "Buy"
        else:
            sl = entry * (1 + SL_PCT / 100)
            tp = entry * (1 - TP_PCT / 100)
            side = "Sell"

        # Valideaza SL
        ok, sl_pct, reason = validate_sl(entry, sl)
        if not ok:
            print(f"  [{self.name}] SKIP {direction} — {reason}")
            return

        # Qty — din ACCOUNT INITIAL (nu compound — precum cere specificatia)
        account_init = ctx.state.initial_account
        snap = ex.sizing_snapshot(
            balance=account_init,
            risk_frac=RISK_FRAC,
            entry_price=entry,
            sl_price=sl,
        )
        qty = snap["qty"]
        if qty <= 0:
            print(f"  [{self.name}] SKIP {direction} — qty=0")
            return

        # Plaseaza market order (Market pt simplicitate in exemplu;
        # productie: foloseste stop-limit pt maker fee)
        order_id = await ex.place_market(self.symbol, side, qty)
        if not order_id:
            print(f"  [{self.name}] order FAILED")
            return

        # Seteaza SL pe pozitie
        await ex.set_position_sl(self.symbol, sl)

        # Store trade state
        self._in_trade    = True
        self._dir         = direction
        self._entry_ts    = ts * 1000       # ms UTC
        self._entry_price = entry
        self._sl_price    = sl
        self._tp_price    = tp
        self._qty         = qty

        dir_icon = "🚀" if direction == "LONG" else "📉"
        await ctx.send_telegram(
            f"{dir_icon} ENTRY {direction}",
            f"<b>Strategy:</b> <code>{self.name}</code>\n"
            f"Entry:    {entry:.2f}\n"
            f"SL:       {sl:.2f}  ({snap['sl_pct']:.3f}%)\n"
            f"TP:       {tp:.2f}  ({TP_PCT}%)\n"
            f"Qty:      {qty} {self.symbol[:-4]}\n"
            f"Notional: ${snap['actual_notional']:.2f}\n"
            f"Risk:     ${snap['actual_risk']:.2f}  ({RISK_FRAC*100:.0f}% din ${account_init:.0f})"
        )

    # ------------------------------------------------------------------
    # Check SL / TP (pe live ticks)
    # ------------------------------------------------------------------
    async def _check_sl_tp(self, ctx: StrategyContext,
                           ts: int, h: float, l: float, c: float) -> None:
        if self._dir == "LONG":
            sl_hit = l <= self._sl_price
            tp_hit = h >= self._tp_price
        else:
            sl_hit = h >= self._sl_price
            tp_hit = l <= self._tp_price

        if not sl_hit and not tp_hit:
            return

        exit_reason       = "SL" if sl_hit else "TP"
        exit_price_target = self._sl_price if sl_hit else self._tp_price

        # NOTE: framework-ul (record_closed_trade) face reconcilierea cu Bybit:
        #   - SL hit: confirma ca stop-market-ul s-a triggerit; daca nu, force.
        #   - TP hit: chase_close + verifica ca a inchis (sau forteaza).
        # Nu mai apelam chase_close aici — duplicare cu reconcilierea.
        from main import record_closed_trade
        try:
            await record_closed_trade(
                direction=self._dir,
                entry_ts_ms=self._entry_ts,
                entry_price=self._entry_price,
                sl_price=self._sl_price,
                tp_price=self._tp_price,
                qty=self._qty,
                exit_ts_ms=ts * 1000,
                exit_price_target=exit_price_target,
                exit_reason=exit_reason,
                extra={"strategy": self.name},
            )
        except ReconciliationError as e:
            # qty pe Bybit > qty local — anomalie. Telegram critic deja trimis.
            # Strategia ramane "ocupata" (_in_trade=True) si HALTED, ca sa nu
            # plaseze ordine peste o stare pe care nu o intelegem.
            print(f"  [{self.name}] HALTED: {e}")
            self._halted = True
            return

        # Reset state DOAR daca reconcilierea a confirmat inchiderea.
        self._in_trade = False
        self._dir = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _update_emas(self, close: float) -> None:
        """EMA incrementala (dupa ce am seed din istoric)."""
        if self._ema_fast is None:
            if len(self._closes) >= EMA_FAST:
                self._ema_fast = sum(list(self._closes)[-EMA_FAST:]) / EMA_FAST
        else:
            k = 2 / (EMA_FAST + 1)
            self._ema_fast = close * k + self._ema_fast * (1 - k)

        if self._ema_slow is None:
            if len(self._closes) >= EMA_SLOW:
                self._ema_slow = sum(list(self._closes)[-EMA_SLOW:]) / EMA_SLOW
        else:
            k = 2 / (EMA_SLOW + 1)
            self._ema_slow = close * k + self._ema_slow * (1 - k)
