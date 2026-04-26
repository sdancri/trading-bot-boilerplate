"""
bybit_trader.py — Bybit V5 API client (pentru boilerplate)
============================================================

⚠️  FOLOSEȘTE httpx DIRECT (NU ccxt) — DELIBERAT.
   Calls explicite cu category=linear pe fiecare endpoint = rapid + predictibil.
   Dacă forkuiești și înlocuiești cu ccxt.bybit, citește secțiunea
   "Warning: dacă înlocuiești httpx cu ccxt" din README — există capcane
   (load_markets timeouts, fetch_currencies private endpoint, restart loops)
   care pot face bot-ul să crash în loop la startup pe VPS-uri.

Functii disponibile pentru strategii:

  Market data:
    get_ticker(symbol)                    -> {bid1, ask1, last, ...}
    get_kline(symbol, interval, limit)    -> list[dict]

  Account:
    get_balance()                         -> float (USDT disponibil)
    get_position_qty(symbol)              -> float (BTC cantitate)

  Orders:
    place_stop_limit(...)                 -> order_id
    place_limit_postonly(...)             -> order_id
    place_market(...)                     -> order_id
    cancel_order(symbol, order_id)
    cancel_all_stops(symbol)
    set_position_sl(symbol, sl_price)

  PnL & history (CRUCIAL pt boilerplate):
    fetch_closed_pnl(symbol, after_ts_ms) -> list[dict] cu closedPnl REAL
    fetch_last_closed_pnl(symbol)         -> ultimul trade inchis (pt notificare)

  Helpers:
    calc_qty_by_risk(balance, risk_frac, sl_dist)

Env vars:
    BYBIT_API_KEY      — obligatoriu
    BYBIT_API_SECRET   — obligatoriu
    BYBIT_TESTNET      — "1" pentru testnet
    BYBIT_CATEGORY     — "linear" (default) sau "inverse"
    QTY_STEP           — ex "0.001" (BTCUSDT perp)
    QTY_PRECISION      — ex "3" (nr zecimale pt qty)
    PRICE_PRECISION    — ex "2" (nr zecimale pt price)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import time
import urllib.parse
from typing import Optional

import httpx


# ----------------------------------------------------------------------------
# Config (citit din env la fiecare apel — nu la import — ca sa mearga .env files)
# ----------------------------------------------------------------------------

def _cat() -> str:
    return os.getenv("BYBIT_CATEGORY", "linear")

def _qty_step() -> float:
    return float(os.getenv("QTY_STEP", "0.001"))

def _qty_prec() -> int:
    return int(os.getenv("QTY_PRECISION", "3"))

def _price_prec() -> int:
    return int(os.getenv("PRICE_PRECISION", "2"))

def _base() -> str:
    return "https://api-testnet.bybit.com" if os.getenv("BYBIT_TESTNET", "0") == "1" \
           else "https://api.bybit.com"

def _creds() -> tuple[str, str]:
    return os.getenv("BYBIT_API_KEY", ""), os.getenv("BYBIT_API_SECRET", "")


# ----------------------------------------------------------------------------
# Signing (Bybit V5)
# ----------------------------------------------------------------------------

def _sign(key: str, secret: str, payload: str) -> dict:
    ts   = str(int(time.time() * 1000))
    recv = "5000"
    msg  = ts + key + recv + payload
    sig  = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {
        "X-BAPI-API-KEY":     key,
        "X-BAPI-TIMESTAMP":   ts,
        "X-BAPI-SIGN":        sig,
        "X-BAPI-RECV-WINDOW": recv,
        "Content-Type":       "application/json",
    }


async def _post(endpoint: str, body: dict) -> Optional[dict]:
    key, secret = _creds()
    if not key or not secret:
        print(f"  [BYBIT] API keys not set — skip {endpoint}")
        return None
    body_str = json.dumps(body)
    try:
        # Rate limiter — protejeaza contra 10006 / IP ban
        import core.rate_limiter as rl
        await rl.wait_token()
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{_base()}{endpoint}",
                             headers=_sign(key, secret, body_str),
                             content=body_str)
            d = r.json()
        if d.get("retCode") != 0:
            print(f"  [BYBIT] {endpoint}  {d['retCode']}: {d['retMsg']}")
            return None
        return d.get("result")
    except Exception as e:
        print(f"  [BYBIT] {endpoint} error: {e}")
        return None


async def _get(endpoint: str, params: dict, signed: bool = True) -> Optional[dict]:
    key, secret = _creds()
    try:
        # Rate limiter — chiar si pt unsigned (public market data) pt consistenta
        import core.rate_limiter as rl
        await rl.wait_token()
        async with httpx.AsyncClient(timeout=10) as c:
            if signed:
                if not key or not secret:
                    return None
                qs = urllib.parse.urlencode(params)
                r = await c.get(f"{_base()}{endpoint}",
                                headers=_sign(key, secret, qs),
                                params=params)
            else:
                r = await c.get(f"{_base()}{endpoint}", params=params)
            d = r.json()
        if d.get("retCode") != 0:
            print(f"  [BYBIT] {endpoint}  {d['retCode']}: {d['retMsg']}")
            return None
        return d.get("result")
    except Exception as e:
        print(f"  [BYBIT] {endpoint} error: {e}")
        return None


# ============================================================================
# Market data (public — nu are nevoie de API key)
# ============================================================================

async def get_ticker(symbol: str) -> Optional[dict]:
    r = await _get("/v5/market/tickers",
                   {"category": _cat(), "symbol": symbol},
                   signed=False)
    if not r:
        return None
    try:
        t = r["list"][0]
        return {
            "last": float(t["lastPrice"]),
            "bid1": float(t["bid1Price"]),
            "ask1": float(t["ask1Price"]),
            "mark": float(t.get("markPrice", t["lastPrice"])),
        }
    except Exception:
        return None


async def get_kline(symbol: str, interval: str, limit: int = 1000,
                    start: Optional[int] = None,
                    end:   Optional[int] = None) -> list[list]:
    """
    interval: "1","3","5","15","30","60","120","240","360","720","D","W","M"
    Returns: list[[ts_ms, open, high, low, close, volume, turnover]] DESC ordonat.
    """
    params = {"category": _cat(), "symbol": symbol,
              "interval": interval, "limit": limit}
    if start is not None:
        params["start"] = int(start)
    if end is not None:
        params["end"] = int(end)
    r = await _get("/v5/market/kline", params, signed=False)
    return r.get("list", []) if r else []


# ============================================================================
# Account
# ============================================================================

async def get_balance() -> Optional[float]:
    """
    USDT disponibil — conturi UNIFIED.

    ⚠️  DEBUG-ONLY. NU folosi pt equity/sizing.
    Bot-ul are propriul equity local in BotState.account care se actualizeaza
    STRICT cu PnL-ul real tras de pe Bybit dupa fiecare trade inchis.
    Aceasta functie este expusa DOAR pt debugging manual (ex: verificare
    ad-hoc a balance-ului real vs equity-ul local pt detectare drift).
    """
    r = await _get("/v5/account/wallet-balance",
                   {"accountType": "UNIFIED", "coin": "USDT"})
    if not r:
        return None
    try:
        for coin in r["list"][0]["coin"]:
            if coin["coin"] == "USDT":
                return float(coin["availableToWithdraw"] or coin["walletBalance"])
    except Exception:
        pass
    return None


async def get_position_qty(symbol: str) -> float:
    r = await _get("/v5/position/list",
                   {"category": _cat(), "symbol": symbol})
    if not r:
        return 0.0
    try:
        for p in r.get("list", []):
            if p["symbol"] == symbol:
                return float(p.get("size", 0))
    except Exception:
        pass
    return 0.0


# ============================================================================
# PnL & History — INIMA boilerplate-ului
# ============================================================================

async def fetch_closed_pnl(symbol: str,
                           start_ms: Optional[int] = None,
                           limit:    int = 50) -> list[dict]:
    """
    Intoarce lista de trade-uri INCHISE cu PnL-ul REAL (incl. fees).

    Field-uri Bybit returnate (extras din raw):
        closedPnl       — PnL net in USDT (dupa fees)
        cumEntryValue   — valoarea totala a intrarii
        cumExitValue    — valoarea totala a iesirii
        avgEntryPrice   — pret mediu de intrare
        avgExitPrice    — pret mediu de iesire
        qty, orderType, side, createdTime, updatedTime
        execType        — Trade / Funding / etc

    Atentie: daca faci piramidari, un singur "trade logical" poate aparea ca
    MAI MULTE inregistrari in closed-pnl (una per fill). Suma closedPnl-urilor
    din intervalul de timp al trade-ului logical iti da PnL-ul total.
    """
    params = {"category": _cat(), "symbol": symbol, "limit": min(limit, 100)}
    if start_ms:
        params["startTime"] = int(start_ms)
    r = await _get("/v5/position/closed-pnl", params)
    if not r:
        return []
    return r.get("list", [])


async def fetch_pnl_for_trade(symbol: str,
                              entry_ts_ms: int,
                              exit_ts_ms:  int,
                              settle_delay_sec: float = 2.0) -> dict:
    """
    Trage PnL-ul total pentru UN trade logical, inclusiv piramidari.

    Algoritm:
      1. Asteapta `settle_delay_sec` — Bybit nevoie de cateva secunde pana
         inregistreaza closed-pnl pe endpoint
      2. Fetch closed-pnl cu startTime = entry_ts_ms - 1 minut (marja)
      3. Suma `closedPnl` pentru toate inregistrarile cu updatedTime <= exit + 2min

    Returneaza:
      {
        "pnl":          float,   # net dupa fees (principal + piramide)
        "fees":         float,   # fees totale (estimate din qty * price * fee_rate)
        "n_fills":      int,     # cate inregistrari closed-pnl s-au agregat
        "avg_entry":    float,   # pret mediu de intrare (weighted)
        "avg_exit":     float,   # pret mediu de iesire (weighted)
        "raw":          list,    # raw records (pt debug)
      }
    """
    if settle_delay_sec > 0:
        await asyncio.sleep(settle_delay_sec)

    # Marja: incepem cu 60s inainte de entry, terminam cu 120s dupa exit
    start_ms = entry_ts_ms - 60_000
    end_limit_ms = exit_ts_ms + 120_000

    records = await fetch_closed_pnl(symbol, start_ms=start_ms, limit=50)

    relevant = [
        r for r in records
        if start_ms <= int(r.get("updatedTime", 0)) <= end_limit_ms
    ]

    if not relevant:
        print(f"  [BYBIT] WARNING: niciun closed-pnl pentru trade "
              f"{entry_ts_ms}-{exit_ts_ms}  (size-ul ar putea fi 0)")
        return {"pnl": 0.0, "fees": 0.0, "n_fills": 0,
                "avg_entry": 0.0, "avg_exit": 0.0, "raw": []}

    pnl_total = sum(float(r["closedPnl"]) for r in relevant)
    qty_total = sum(float(r["qty"])       for r in relevant)

    avg_entry = (sum(float(r["avgEntryPrice"]) * float(r["qty"]) for r in relevant)
                 / qty_total) if qty_total else 0.0
    avg_exit  = (sum(float(r["avgExitPrice"])  * float(r["qty"]) for r in relevant)
                 / qty_total) if qty_total else 0.0

    # Fees: Bybit nu returneaza direct fees pe endpoint-ul asta — il estimam
    # din diferenta dintre cumEntryValue si cumExitValue daca este populat
    fees = 0.0
    for r in relevant:
        try:
            entry_v = float(r.get("cumEntryValue",  0))
            exit_v  = float(r.get("cumExitValue",   0))
            closed_pnl = float(r["closedPnl"])
            # raw_pnl = exit - entry (pt LONG) sau entry - exit (pt SHORT)
            # fees = raw_pnl - closedPnl
            side = r.get("side", "Buy")
            raw_pnl = (exit_v - entry_v) if side == "Buy" else (entry_v - exit_v)
            fees += abs(raw_pnl - closed_pnl)
        except Exception:
            pass

    return {
        "pnl":       round(pnl_total, 4),
        "fees":      round(fees, 4),
        "n_fills":   len(relevant),
        "avg_entry": round(avg_entry, 4),
        "avg_exit":  round(avg_exit, 4),
        "raw":       relevant,
    }


# ============================================================================
# Orders
# ============================================================================

def calc_qty_by_risk(balance: float, risk_frac: float, sl_dist: float,
                     entry_price: Optional[float] = None) -> float:
    """
    qty = (balance * risk_frac) / sl_dist  — formula clasica in absoluti.

    LEVERAGE NU INTRA IN CALCUL. Formula echivalenta:
        notional = risk$ * 100 / SL%
        qty      = notional / price

    Pastrat cu aceasta signatura pt compat cu codul ORB vechi.
    Daca `entry_price` e dat, deleg la position_sizing.qty_by_risk (mai robust
    la rotunjiri).
    """
    if sl_dist <= 0 or risk_frac <= 0 or balance <= 0:
        return 0.0

    if entry_price and entry_price > 0:
        import core.position_sizing as ps
        return ps.qty_by_risk(
            balance=balance,
            risk_frac=risk_frac,
            entry_price=entry_price,
            sl_price=entry_price - sl_dist,   # direction-agnostic — |dist|
            qty_step=_qty_step(),
            qty_precision=_qty_prec(),
        )

    # Legacy path — fara entry_price
    raw = (balance * risk_frac) / sl_dist
    step = _qty_step()
    return round(math.floor(raw / step) * step, _qty_prec())


def sizing_snapshot(balance: float, risk_frac: float,
                    entry_price: float, sl_price: float) -> dict:
    """Snapshot complet pt logging/Telegram (fara leverage)."""
    import core.position_sizing as ps
    return ps.sizing_snapshot(
        balance=balance, risk_frac=risk_frac,
        entry_price=entry_price, sl_price=sl_price,
        qty_step=_qty_step(), qty_precision=_qty_prec(),
    )


def _fmt_price(p: float) -> str:
    return f"{p:.{_price_prec()}f}"


def _fmt_qty(q: float) -> str:
    return f"{q:.{_qty_prec()}f}"


async def place_stop_limit(symbol:    str,
                           side:      str,          # "Buy" / "Sell"
                           price:     float,
                           qty:       float,
                           trigger:   float,
                           direction: int           # 1 = rise to trig, 2 = fall to trig
                           ) -> Optional[str]:
    r = await _post("/v5/order/create", {
        "category":         _cat(),
        "symbol":           symbol,
        "side":             side,
        "orderType":        "Limit",
        "price":            _fmt_price(price),
        "qty":              _fmt_qty(qty),
        "triggerPrice":     _fmt_price(trigger),
        "triggerBy":        "LastPrice",
        "triggerDirection": direction,
        "orderFilter":      "StopOrder",
        "timeInForce":      "GTC",
    })
    return r.get("orderId") if r else None


async def place_limit_postonly(symbol:      str,
                               side:        str,
                               price:       float,
                               qty:         float,
                               reduce_only: bool = False
                               ) -> Optional[str]:
    r = await _post("/v5/order/create", {
        "category":    _cat(),
        "symbol":      symbol,
        "side":        side,
        "orderType":   "Limit",
        "price":       _fmt_price(price),
        "qty":         _fmt_qty(qty),
        "timeInForce": "PostOnly",
        "reduceOnly":  reduce_only,
    })
    return r.get("orderId") if r else None


async def place_market(symbol:      str,
                       side:        str,
                       qty:         float,
                       reduce_only: bool = False) -> Optional[str]:
    r = await _post("/v5/order/create", {
        "category":    _cat(),
        "symbol":      symbol,
        "side":        side,
        "orderType":   "Market",
        "qty":         _fmt_qty(qty),
        "timeInForce": "IOC",
        "reduceOnly":  reduce_only,
    })
    return r.get("orderId") if r else None


async def cancel_order(symbol: str, order_id: Optional[str]) -> None:
    if not order_id:
        return
    await _post("/v5/order/cancel", {
        "category": _cat(), "symbol": symbol, "orderId": order_id,
    })


async def cancel_all_stops(symbol: str) -> None:
    await _post("/v5/order/cancel-all", {
        "category":    _cat(),
        "symbol":      symbol,
        "orderFilter": "StopOrder",
    })


async def set_position_sl(symbol: str, sl_price: float) -> None:
    await _post("/v5/position/set-trading-stop", {
        "category":    _cat(),
        "symbol":      symbol,
        "stopLoss":    _fmt_price(sl_price),
        "slTriggerBy": "LastPrice",
        "positionIdx": 0,
    })


# ============================================================================
# Chase-close (maker close pt exit) — reluat din boilerplate-ul ORB
# ============================================================================

async def chase_close(symbol: str, direction: str,
                      max_attempts: int = 20,
                      interval_sec: float = 3.0) -> None:
    """Inchide pozitia cu limit maker (reduce-only PostOnly); fallback market."""
    await cancel_all_stops(symbol)
    close_side = "Sell" if direction == "LONG" else "Buy"
    last_id: Optional[str] = None

    for attempt in range(max_attempts):
        qty = await get_position_qty(symbol)
        if qty <= 0:
            print(f"  [BYBIT] Chase close: pozitie inchisa ({attempt} incercari)")
            return

        if last_id:
            await cancel_order(symbol, last_id)
            last_id = None

        t = await get_ticker(symbol)
        if not t:
            await asyncio.sleep(interval_sec)
            continue

        price = t["ask1"] if direction == "LONG" else t["bid1"]
        last_id = await place_limit_postonly(symbol, close_side, price, qty,
                                             reduce_only=True)
        if last_id:
            print(f"  [BYBIT] Chase {attempt+1}/{max_attempts}: "
                  f"{close_side} @ {price:.{_price_prec()}f} qty={qty}")
        await asyncio.sleep(interval_sec)

    # Fallback market
    qty = await get_position_qty(symbol)
    if qty > 0:
        if last_id:
            await cancel_order(symbol, last_id)
        print(f"  [BYBIT] Chase close FAILED — fallback MARKET {qty}")
        await place_market(symbol, close_side, qty, reduce_only=True)
