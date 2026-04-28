"""
position_sizing.py — Formule position sizing
=================================================

REGULA:
  Leverage NU afecteaza marimea pozitiei. Formula sizing:
      notional = risk_amount * 100 / SL%
      qty      = notional / entry_price

  LEVERAGE_MAX (env) intra DOAR ca plafon de siguranta — botul cap-eaza
  qty-ul ca Bybit sa nu refuze ordinul cand notional > balance × leverage.
  Cap formula:
      notional_max = cap_pct × bybit_balance × LEVERAGE_MAX   (cap_pct=0.95)
  USER-UL TREBUIE sa fi setat manual leverage-ul = LEVERAGE_MAX pe simbol
  in UI Bybit. Botul nu il schimba via API.

FORMULELE:

  risk_amount [USDT] = balance * risk_frac
                       ex: 100$ * 5% = 5$ risk per trade

  SL%                = |entry - sl| / entry * 100
                       ex: entry 80000, sl 79720 -> 0.35%

  notional [USDT]    = risk_amount * 100 / SL%
                       ex: 5 * 100 / 0.35 = $1428.57

  notional_capped    = min(notional, 0.95 × bybit_balance × LEVERAGE_MAX)

  qty                = notional_capped / entry_price   (rotunjit JOS la qty_step)

  PnL la SL          = -risk_amount  (numai daca nu s-a aplicat capul)
                       Daca capul a redus notional, risk-ul efectiv scade
                       proportional.
"""
from __future__ import annotations

import math
import os


CAP_PCT_DEFAULT = 0.95


def _leverage_max() -> float:
    """LEVERAGE_MAX din env. Fallback 1.0 (no leverage) daca lipseste."""
    try:
        return float(os.getenv("LEVERAGE_MAX", "1") or "1")
    except ValueError:
        return 1.0


def risk_amount(balance: float, risk_frac: float) -> float:
    """risk_amount = balance * risk_frac  [USDT]."""
    if balance <= 0 or risk_frac <= 0:
        return 0.0
    return balance * risk_frac


def sl_pct(entry_price: float, sl_price: float) -> float:
    """SL% = |entry - sl| / entry * 100."""
    if entry_price <= 0:
        return 0.0
    return abs(entry_price - sl_price) / entry_price * 100


def notional_from_risk(risk_amt: float, sl_pct_val: float) -> float:
    """notional = risk * 100 / SL%   (formula reala — fara cap)."""
    if risk_amt <= 0 or sl_pct_val <= 0:
        return 0.0
    return risk_amt * 100 / sl_pct_val


def max_notional(bybit_balance: float,
                 leverage_max: float,
                 cap_pct: float = CAP_PCT_DEFAULT) -> float:
    """
    Plafon notional ca Bybit sa nu refuze ordinul:
        max_notional = cap_pct × bybit_balance × leverage_max
    Returneaza 0 daca oricare input e <=0 (no cap aplicabil → caller-ul
    foloseste notional original).
    """
    if bybit_balance <= 0 or leverage_max <= 0 or cap_pct <= 0:
        return 0.0
    return cap_pct * bybit_balance * leverage_max


def qty_from_notional(notional: float, price: float,
                      qty_step: float = 0.001,
                      qty_precision: int = 3) -> float:
    """qty = notional / price, rotunjit JOS la qty_step."""
    if price <= 0 or notional <= 0 or qty_step <= 0:
        return 0.0
    raw = notional / price
    return round(math.floor(raw / qty_step) * qty_step, qty_precision)


def qty_by_risk(balance:        float,
                risk_frac:      float,
                entry_price:    float,
                sl_price:       float,
                qty_step:       float = 0.001,
                qty_precision:  int   = 3,
                bybit_balance:  float | None = None,
                leverage_max:   float | None = None,
                cap_pct:        float = CAP_PCT_DEFAULT) -> float:
    """
    Pipeline: balance + risk% + entry + SL  ->  qty (rotunjit la lot size).

    Capul automat se aplica daca `bybit_balance` e dat (>0):
        notional_used = min(notional_dorit, cap_pct × bybit_balance × leverage_max)

    Daca `leverage_max` lipseste, se citeste LEVERAGE_MAX din env.
    Daca `bybit_balance` e None / 0, capul NU se aplica (qty pur teoretic).
    """
    ra       = risk_amount(balance, risk_frac)
    slp      = sl_pct(entry_price, sl_price)
    notional = notional_from_risk(ra, slp)

    if bybit_balance and bybit_balance > 0:
        lev = leverage_max if leverage_max is not None else _leverage_max()
        cap = max_notional(bybit_balance, lev, cap_pct)
        if cap > 0 and notional > cap:
            notional = cap

    return qty_from_notional(notional, entry_price, qty_step, qty_precision)


def sizing_snapshot(balance:        float,
                    risk_frac:      float,
                    entry_price:    float,
                    sl_price:       float,
                    qty_step:       float = 0.001,
                    qty_precision:  int   = 3,
                    bybit_balance:  float | None = None,
                    leverage_max:   float | None = None,
                    cap_pct:        float = CAP_PCT_DEFAULT) -> dict:
    """
    Dict complet cu toate numerele relevante — pt logging / Telegram.

    Camp `capped` = True daca notional dorit > cap_max si qty a fost redus.
    """
    ra       = risk_amount(balance, risk_frac)
    slp      = sl_pct(entry_price, sl_price)
    ntl      = notional_from_risk(ra, slp)

    lev      = leverage_max if leverage_max is not None else _leverage_max()
    cap      = max_notional(bybit_balance or 0.0, lev, cap_pct)
    capped   = bool(cap > 0 and ntl > cap)
    ntl_used = cap if capped else ntl

    qty      = qty_from_notional(ntl_used, entry_price, qty_step, qty_precision)
    a_ntl    = qty * entry_price
    a_risk   = a_ntl * slp / 100

    return {
        "balance":         round(balance, 4),
        "risk_frac":       round(risk_frac, 4),
        "risk_amount":     round(ra, 4),
        "sl_pct":          round(slp, 4),
        "entry_price":     entry_price,
        "sl_price":        sl_price,
        "notional":        round(ntl, 2),
        "max_notional":    round(cap, 2),
        "leverage_max":    lev,
        "bybit_balance":   round(bybit_balance, 2) if bybit_balance else 0.0,
        "capped":          capped,
        "qty":             qty,
        "actual_notional": round(a_ntl, 2),
        "actual_risk":     round(a_risk, 4),
    }
