"""
position_sizing.py — Formule position sizing
=================================================

REGULA:
  Leverage NU afecteaza marimea pozitiei. Nu apare in NICIUN calcul de aici.
  (Leverage afecteaza doar marja blocata pe cont — separate, nerelevant pt
   calculul qty / notional / risk.)

FORMULELE:

  risk_amount [USDT] = balance * risk_frac
                       ex: 100$ * 5% = 5$ risk per trade

  SL%                = |entry - sl| / entry * 100
                       ex: entry 80000, sl 79720 -> 0.35%

  notional [USDT]    = risk_amount * 100 / SL%
                       ex: 5 * 100 / 0.35 = $1428.57
                       (NU exista cap din leverage)

  qty                = notional / entry_price   (rotunjit JOS la qty_step)
                       ex: 1428.57 / 80000 = 0.01786 -> 0.017 BTC

  PnL la SL          = -risk_amount
                       (indiferent de leverage — confirma ca leverage nu
                        influenteaza pierderea maxima)
"""
from __future__ import annotations

import math


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


def qty_from_notional(notional: float, price: float,
                      qty_step: float = 0.001,
                      qty_precision: int = 3) -> float:
    """qty = notional / price, rotunjit JOS la qty_step."""
    if price <= 0 or notional <= 0 or qty_step <= 0:
        return 0.0
    raw = notional / price
    return round(math.floor(raw / qty_step) * qty_step, qty_precision)


def qty_by_risk(balance:       float,
                risk_frac:     float,
                entry_price:   float,
                sl_price:      float,
                qty_step:      float = 0.001,
                qty_precision: int   = 3) -> float:
    """
    Pipeline: balance + risk% + entry + SL  ->  qty (rotunjit la lot size).

    Echivalent cu: risk$ -> notional$ -> qty
    """
    ra       = risk_amount(balance, risk_frac)
    slp      = sl_pct(entry_price, sl_price)
    notional = notional_from_risk(ra, slp)
    return qty_from_notional(notional, entry_price, qty_step, qty_precision)


def sizing_snapshot(balance:       float,
                    risk_frac:     float,
                    entry_price:   float,
                    sl_price:      float,
                    qty_step:      float = 0.001,
                    qty_precision: int   = 3) -> dict:
    """
    Dict complet cu toate numerele relevante — pt logging / Telegram.

    Example:
        snap = sizing_snapshot(100, 0.05, 80000, 79720)
        # {
        #   "balance":         100,
        #   "risk_frac":       0.05,
        #   "risk_amount":     5.0,
        #   "sl_pct":          0.35,
        #   "entry_price":     80000,
        #   "sl_price":        79720,
        #   "notional":        1428.57,         # teoretic
        #   "qty":             0.017,
        #   "actual_notional": 1360.0,          # = qty * entry (dupa rounding)
        #   "actual_risk":     4.76,            # = actual_notional * SL%/100
        # }
    """
    ra     = risk_amount(balance, risk_frac)
    slp    = sl_pct(entry_price, sl_price)
    ntl    = notional_from_risk(ra, slp)
    qty    = qty_from_notional(ntl, entry_price, qty_step, qty_precision)
    a_ntl  = qty * entry_price
    a_risk = a_ntl * slp / 100

    return {
        "balance":         round(balance, 4),
        "risk_frac":       round(risk_frac, 4),
        "risk_amount":     round(ra, 4),
        "sl_pct":          round(slp, 4),
        "entry_price":     entry_price,
        "sl_price":        sl_price,
        "notional":        round(ntl, 2),
        "qty":             qty,
        "actual_notional": round(a_ntl, 2),
        "actual_risk":     round(a_risk, 4),
    }
