"""
bot_state.py — State management pentru bot
==========================================
Tine state-ul botului (account, trades, equity_curve, indicators) si optional
il persista pe disk in ${DATA_DIR}/bot_state.json (default DATA_DIR=/data).

  - account: porneste la ACCOUNT_SIZE ($100) si creste/scade DOAR cu
    PnL-ul real tras de pe Bybit dupa fiecare trade inchis.
  - trades: lista de traduri inchise (pt panelul lateral al chart-ului)
  - equity_curve: (timestamp, equity) dupa fiecare trade
  - first_candle_ts: timestamp-ul primei lumanari primite dupa start

PERSISTENTA + RESET_TOKEN:
  Daca DATA_DIR exista, BotState.load() incarca state-ul anterior din
  ${DATA_DIR}/bot_state.json si BotState.save() il scrie inapoi.

  Mecanism reset controlat: env var `RESET_TOKEN`. Token-ul se salveaza
  in fisier; daca env-ul difera de cel stocat la urmatorul start, statul
  e curatat (history=[], account=initial_account) si se rescrie cu noul
  token. Lasa env var-ul gol -> niciodata reset. Schimba-i valoarea
  (ex: v1 -> v2) ca sa fortezi reset la urmatoarea pornire.

  Safe la crash-restart loops: token-ul ramane acelasi cat timp env-ul
  nu se schimba.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


ACCOUNT_SIZE = float(os.getenv("ACCOUNT_SIZE", "100.0"))
DATA_DIR     = os.getenv("DATA_DIR", "")           # "" -> persistenta dezactivata
RESET_TOKEN  = os.getenv("RESET_TOKEN", "")        # "" -> niciodata reset


class ReconciliationError(Exception):
    """
    Stare divergenta intre starea locala a bot-ului si pozitia reala de pe Bybit.
    Ridicata de record_closed_trade cand qty_real > qty_local — situatie care
    NU se rezolva safe automat (vezi: piramidare necontorizata, pozitie reziduala
    dintr-un run anterior, interventie manuala). Strategia trebuie sa intre in
    HALT pe simbolul afectat pana la review manual + restart.
    """
    pass


@dataclass
class TradeRecord:
    """Trade inchis — PnL-ul vine REAL de pe Bybit.

    exit_price        — pretul ACTUAL la care s-a iesit (avg_exit din Bybit
                        closed-pnl). Pe FORCED/PARTIAL include weighted average
                        peste fill-ul partial + chase_close.
    exit_price_target — pretul VIZAT de strategie (self._sl_price / _tp_price).
                        Diferenta dintre cele doua = slippage real vs plan.
    """
    id:           int
    date:         str                 # "YYYY-MM-DD"
    direction:    str                 # "LONG" / "SHORT"
    entry_ts:     int                 # ms UTC
    entry_price:  float
    sl_price:     float
    tp_price:     Optional[float]     # None daca strategia nu are TP (ex ORB)
    qty:          float
    exit_ts:      int
    exit_price:   float                # ACTUAL exit (avg_exit Bybit)
    exit_reason:  str                  # "TP" / "SL" / "SL_FORCED" / "TP_FORCED" / "SL_PARTIAL" / ...
    pnl:          float                # USDT — TRAS DE PE BYBIT (closed-pnl endpoint)
    fees:         float                = 0.0
    exit_price_target: float           = 0.0   # SL/TP vizat de strategie (0 = legacy/necunoscut)
    extra:        dict                 = field(default_factory=dict)  # meta strategia

    @property
    def slippage(self) -> float:
        """Slippage vs target. Conventie: pozitiv = mai prost decat target,
        negativ = mai bun. Aplicabil la LONG si SHORT uniform.
        Returneaza 0.0 daca target sau actual lipsesc (legacy records)."""
        if self.exit_price_target <= 0 or self.exit_price <= 0:
            return 0.0
        if self.direction == "LONG":
            return self.exit_price_target - self.exit_price
        return self.exit_price - self.exit_price_target

    def to_dict(self) -> dict:
        """Format pentru chart_template.py & JSON API."""
        return {
            "id":          self.id,
            "date":        self.date,
            "direction":   self.direction,
            "side":        "L" if self.direction == "LONG" else "S",
            "entry_ms":    self.entry_ts,
            "entry_price": round(self.entry_price, 4),
            "sl":          round(self.sl_price, 4),
            "tp":          round(self.tp_price, 4) if self.tp_price else 0,
            "qty":         round(self.qty, 6),
            "size_usdt":   round(self.qty * self.entry_price, 2),
            "exit_ms":     self.exit_ts,
            "exit_price":  round(self.exit_price, 4),
            "exit_price_target": round(self.exit_price_target, 4),
            "slippage":    round(self.slippage, 4),
            "exit_reason": self.exit_reason,
            "pnl":         round(self.pnl, 4),
            "fees":        round(self.fees, 4),
            "extra":       self.extra,
        }

    def to_persist(self) -> dict:
        """Format pentru persistenta pe disk (toate campurile, fara round)."""
        return {
            "id":          self.id,
            "date":        self.date,
            "direction":   self.direction,
            "entry_ts":    self.entry_ts,
            "entry_price": self.entry_price,
            "sl_price":    self.sl_price,
            "tp_price":    self.tp_price,
            "qty":         self.qty,
            "exit_ts":     self.exit_ts,
            "exit_price":  self.exit_price,
            "exit_price_target": self.exit_price_target,
            "exit_reason": self.exit_reason,
            "pnl":         self.pnl,
            "fees":        self.fees,
            "extra":       self.extra,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'TradeRecord':
        # Legacy persisted records (pre-reconciliation): foloseau exit_price ca
        # target. Tratam exit_price_target lipsa ca egal cu exit_price -> slippage=0.
        exit_price = d["exit_price"]
        return cls(
            id=d["id"], date=d["date"], direction=d["direction"],
            entry_ts=d["entry_ts"], entry_price=d["entry_price"],
            sl_price=d["sl_price"], tp_price=d.get("tp_price"),
            qty=d["qty"], exit_ts=d["exit_ts"], exit_price=exit_price,
            exit_price_target=d.get("exit_price_target", exit_price),
            exit_reason=d["exit_reason"], pnl=d["pnl"],
            fees=d.get("fees", 0.0), extra=d.get("extra") or {},
        )


class BotState:
    """
    Stare globala bot — reset la restart.

    Campuri capital:

      self.initial_account  — Valoarea de pornire (din env ACCOUNT_SIZE, default
                              100). Constanta pe durata sesiunii. Folosita pt
                              calculul Return% afisat pe chart.

      self.account          — Equity curent. La pornire = initial_account.
                              Dupa fiecare trade inchis: account += trade.pnl
                              (PnL real tras de pe Bybit, fees incluse).

    Cum le folosesti in strategia ta — depinde 100% de tine. Boilerplate-ul
    nu dicteaza daca risk-ul per trade se calculeaza pe initial (fix) sau pe
    account (compound) — e decizia ta, caz cu caz.

    CONTRACT EQUITY (mecanic, nu prescriptiv):
      state.account se actualizeaza DOAR prin:
          state.account += trade.pnl     # trade.pnl = PnL REAL de pe Bybit

      Echivalent:
          account(t) = initial_account + sum(PnL_bybit_i for i in trades_inchise)

      Bot-ul NU interogheaza NICIODATA balance-ul real din contul Bybit.
      (get_balance() din exchange_api este debug-only.)

      Motivatie: equity-ul afisat pe chart reflecta strict performanta
      strategiei, nu e afectat de alte activitati din contul Bybit.

    Single source of truth pt:
      - account (display Account & Return pe chart)
      - trades (lista din panel)
      - equity_curve
      - first_candle_ts (chart-ul afiseaza numai lumanari >= asta)
    """

    def __init__(self, account_size: float = ACCOUNT_SIZE) -> None:
        self.initial_account: float = account_size
        self.account:         float = account_size
        self.trades:          list[TradeRecord] = []
        self.equity_curve:    list[dict] = []   # [{time: s, value: $}]
        self.first_candle_ts: Optional[int] = None   # UTC seconds
        self.start_utc:       datetime = datetime.now(timezone.utc)

        # Indicatori overlay pe chart (ex EMA, BB, VWAP)
        # Warmup se face intern in strategie (pe istoric), iar pe chart se
        # publica DOAR valorile >= first_candle_ts — afisate "mature".
        self.indicators:      dict[str, list[dict]] = {}
        self.indicator_meta:  dict[str, dict] = {}

        # Lock pentru save/load (Python's GIL nu protejeaza file I/O concurent)
        self._lock = threading.Lock()

        # Seed equity curve la start
        first_ts = int(self.start_utc.timestamp())
        self.equity_curve.append({"time": first_ts, "value": round(self.account, 4)})

    # ----------------------------------------------------------------
    # Indicatori
    # ----------------------------------------------------------------
    def register_indicator(self, name: str,
                           color:      str = "#ffd700",
                           line_width: int = 1,
                           line_style: int = 0) -> None:
        """
        Inregistreaza un indicator pt afisare pe chart.
        Se apeleaza o data per indicator, de obicei in strategy.on_start().

        line_style: 0=solid, 1=dotted, 2=dashed, 3=large-dashed, 4=sparse-dotted
        """
        self.indicator_meta[name] = {
            "color":      color,
            "lineWidth":  line_width,
            "lineStyle":  line_style,
        }
        if name not in self.indicators:
            self.indicators[name] = []

    def add_indicator_point(self, name: str, ts_s: int, value: float) -> None:
        """Adauga un punct (ts, value) la seria indicatorului."""
        if name not in self.indicators:
            self.register_indicator(name)
        self.indicators[name].append({
            "time":  int(ts_s),
            "value": round(float(value), 6),
        })
        # Evita memory bloat
        if len(self.indicators[name]) > 20000:
            self.indicators[name].pop(0)

    # ----------------------------------------------------------------
    # Trades
    # ----------------------------------------------------------------
    def add_closed_trade(self, trade: TradeRecord) -> None:
        """
        Adauga un trade INCHIS. PnL-ul TREBUIE sa fie deja tras de pe Bybit
        (vezi bybit_trader.fetch_pnl_for_trade).

        Equity-ul se calculeaza LOCAL:
            self.account += trade.pnl     # nimic tras din balance-ul Bybit!
        """
        trade.id = len(self.trades) + 1
        self.trades.append(trade)
        self.account += trade.pnl                     # local compute — NU Bybit balance
        self.equity_curve.append({
            "time":  trade.exit_ts // 1000,           # ms -> s
            "value": round(self.account, 4),
        })
        print(f"  [STATE] Trade #{trade.id} {trade.direction} "
              f"PnL_bybit=${trade.pnl:+,.2f}  "
              f"Account_local=${self.account:,.2f}")

    # ----------------------------------------------------------------
    # Candles — pentru filtrarea chart-ului
    # ----------------------------------------------------------------
    def mark_first_candle(self, ts: int) -> None:
        """Seteaza timestamp-ul primei lumanari primite via WS. Idempotent."""
        if self.first_candle_ts is None:
            self.first_candle_ts = ts
            print(f"  [STATE] First candle ts = {ts} "
                  f"({datetime.fromtimestamp(ts, tz=timezone.utc)})")

    # ----------------------------------------------------------------
    # Snapshot pentru /api/init
    # ----------------------------------------------------------------
    def summary(self) -> dict:
        n = len(self.trades)
        wins = sum(1 for t in self.trades if t.pnl > 0)
        pnl_total = self.account - self.initial_account
        ret_pct   = (pnl_total / self.initial_account * 100) if self.initial_account else 0.0
        return {
            "initial_account": round(self.initial_account, 2),
            "account":         round(self.account, 2),
            "pnl_total":       round(pnl_total, 2),
            "return_pct":      round(ret_pct, 2),
            "n_trades":        n,
            "n_wins":          wins,
            "n_losses":        n - wins,
            "win_rate":        round(wins / n * 100, 2) if n else 0.0,
            "start_utc":       self.start_utc.isoformat(),
            "uptime_sec":      int((datetime.now(timezone.utc) - self.start_utc).total_seconds()),
        }

    def init_payload(self) -> dict:
        """Payload pentru GET /api/init — folosit de chart la load."""
        return {
            "trades":         [t.to_dict() for t in self.trades],
            "equity":         self.equity_curve,
            "indicators":     self.indicators,
            "indicator_meta": self.indicator_meta,
            "summary":        self.summary(),
            "first_ts":       self.first_candle_ts,
            "bot_name":       os.getenv("BOT_NAME", "bot"),
            "strategy":       os.getenv("STRATEGY_NAME", ""),
            "symbol":         os.getenv("SYMBOL", "BTCUSDT"),
            "timezone":       os.getenv("CHART_TZ", "Europe/Bucharest"),
        }

    # ----------------------------------------------------------------
    # Persistenta pe disk + RESET_TOKEN
    # ----------------------------------------------------------------
    def _state_path(self) -> Optional[str]:
        """Calea fisierului de state. None daca DATA_DIR nu e setat."""
        if not DATA_DIR:
            return None
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
        except OSError as e:
            print(f"  [STATE] WARN: nu pot crea {DATA_DIR}: {e}")
            return None
        return os.path.join(DATA_DIR, "bot_state.json")

    def save(self) -> None:
        """
        Persista state-ul pe disk. Idempotent. Safe la apel concurent.
        Construieste payload-ul sub lock; scrie fisierul OUT-of-lock
        (evita deadlock cu apelanti care deja tin lock).
        """
        path = self._state_path()
        if not path:
            return
        with self._lock:
            data = {
                "initial_account": self.initial_account,
                "account":         self.account,
                "trades":          [t.to_persist() for t in self.trades],
                "equity_curve":    list(self.equity_curve),
                "first_candle_ts": self.first_candle_ts,
                "start_utc":       self.start_utc.isoformat(),
                "indicators":      self.indicators,
                "indicator_meta":  self.indicator_meta,
                "reset_token":     RESET_TOKEN,
            }
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            print(f"  [STATE] save error: {e}")

    def load(self) -> None:
        """
        Incarca state-ul anterior din disk (daca exista).
        Daca RESET_TOKEN env != cel stocat -> wipe + persist empty state.
        Trebuie chemata O DATA la startup, INAINTE de orice alta mutatie.
        """
        path = self._state_path()
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"  [STATE] load error: {e}")
            return

        stored_token = data.get("reset_token", "")
        if RESET_TOKEN and stored_token != RESET_TOKEN:
            print(f"  [STATE] RESET_TOKEN changed ({stored_token!r} -> "
                  f"{RESET_TOKEN!r}) — wiping state, account back to "
                  f"${self.initial_account:,.2f}")
            self.save()    # persist empty state cu noul token
            return

        # Restore complet (state-ul a fost initializat in __init__ cu defaults)
        self.initial_account = data.get("initial_account", self.initial_account)
        self.account         = data.get("account",         self.initial_account)
        self.trades          = [TradeRecord.from_dict(t) for t in data.get("trades", [])]
        self.equity_curve    = data.get("equity_curve", []) or self.equity_curve
        self.first_candle_ts = data.get("first_candle_ts")
        self.indicators      = data.get("indicators", {})     or {}
        self.indicator_meta  = data.get("indicator_meta", {}) or {}
        try:
            self.start_utc = datetime.fromisoformat(data["start_utc"])
        except (KeyError, ValueError):
            pass
        print(f"  [STATE] loaded: account=${self.account:,.2f}  "
              f"trades={len(self.trades)}  "
              f"equity_pts={len(self.equity_curve)}")
