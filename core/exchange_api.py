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
    get_balance()                         -> float (USDT disponibil — pt cap leverage)
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

    Folosit DOAR pt cap-ul de siguranta din position_sizing:
        notional_max = 0.95 × bybit_balance × LEVERAGE_MAX
    Equity-ul afisat pe chart ramane local (state.account += trade.pnl) —
    NU se inlocuieste cu balance-ul real.
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
                     entry_price: Optional[float] = None,
                     bybit_balance: Optional[float] = None) -> float:
    """
    qty = (balance * risk_frac) / sl_dist  — formula clasica in absoluti.

    LEVERAGE NU INTRA IN FORMULA. Echivalent:
        notional = risk$ * 100 / SL%
        qty      = notional / price

    Cap automat (anti-rejection Bybit) cand `entry_price` SI `bybit_balance`
    sunt date:
        notional_capped = min(notional, 0.95 × bybit_balance × LEVERAGE_MAX)
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
            bybit_balance=bybit_balance,
        )

    # Legacy path — fara entry_price (capul nu se aplica)
    raw = (balance * risk_frac) / sl_dist
    step = _qty_step()
    return round(math.floor(raw / step) * step, _qty_prec())


def sizing_snapshot(balance: float, risk_frac: float,
                    entry_price: float, sl_price: float,
                    bybit_balance: Optional[float] = None) -> dict:
    """
    Snapshot complet pt logging/Telegram. Daca pasezi `bybit_balance`, dict-ul
    include si capul `max_notional` + flag-ul `capped`.
    """
    import core.position_sizing as ps
    return ps.sizing_snapshot(
        balance=balance, risk_frac=risk_frac,
        entry_price=entry_price, sl_price=sl_price,
        qty_step=_qty_step(), qty_precision=_qty_prec(),
        bybit_balance=bybit_balance,
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


async def amend_order(symbol: str, order_id: str,
                      price: Optional[float] = None,
                      qty:   Optional[float] = None) -> bool:
    """
    Modifica pretul si/sau qty unui ordin EXISTENT (pastreaza order_id si
    pozitia in queue daca pretul nu s-a schimbat). 1 API call vs 2 (cancel+create).

    Returneaza True daca Bybit a confirmat amendul, False altfel (incl. cazul
    cand ordinul a fost deja umplut sau respins de PostOnly check).
    """
    payload: dict = {"category": _cat(), "symbol": symbol, "orderId": order_id}
    if price is not None:
        payload["price"] = _fmt_price(price)
    if qty is not None:
        payload["qty"] = _fmt_qty(qty)
    r = await _post("/v5/order/amend", payload)
    return r is not None


async def get_order_status(symbol: str, order_id: str) -> Optional[dict]:
    """
    Status detaliat al unui ordin (open sau recent inchis). Returneaza None
    daca ordinul nu mai exista in cache-ul Bybit.

    Field-uri relevante: orderStatus (New/PartiallyFilled/Filled/Cancelled/Rejected),
    cumExecQty, leavesQty, avgPrice, rejectReason.
    """
    r = await _get("/v5/order/realtime",
                   {"category": _cat(), "symbol": symbol, "orderId": order_id})
    if not r or not r.get("list"):
        return None
    return r["list"][0]


async def cancel_all_stops(symbol: str) -> None:
    await _post("/v5/order/cancel-all", {
        "category":    _cat(),
        "symbol":      symbol,
        "orderFilter": "StopOrder",
    })


async def set_position_sl(symbol: str, sl_price: float) -> None:
    """
    Atașează SL la pozitia activa via /v5/position/set-trading-stop.
    SL e implicit Market (slOrderType=Market) — siguranta executiei.

    RECOMANDARE PT STRATEGII CARE STIU SL+TP LA DESCHIDEREA TRADEULUI:
    -----------------------------------------------------------------
    In loc sa monitorizezi TP-ul intra-bar din strategie (pattern v1
    sau chase_close manual), atașează AMBELE SL+TP la pozitie intr-un
    singur call. Bybit gestioneaza totul (cancel-on-close, replace pe
    pyramidari, garantie executie).

    Cheia e `tpOrderType=Limit` cu `tpLimitPrice`: la trigger Bybit
    plaseaza un LIMIT order @ tp_limit_price → maker fee 0.020% in loc
    de 0.055% taker. SL ramane Market pt siguranta.

    Exemplu payload (LONG, entry=$100, SL=$97, TP=$104):

        await _post("/v5/position/set-trading-stop", {
            "category":     "linear",
            "symbol":       "ETHUSDT",
            "positionIdx":  0,
            # SL = market (siguranta)
            "stopLoss":     "97.00",
            "slTriggerBy":  "LastPrice",
            "slOrderType":  "Market",
            # TP = limit (maker fee)
            "takeProfit":   "104.00",
            "tpTriggerBy":  "LastPrice",
            "tpOrderType":  "Limit",
            "tpLimitPrice": "103.95",   # ~0.05% mai prost ca trigger pt fill prob
        })

    Pentru SHORT: simetric, tpLimitPrice cu ~0.05% mai sus decat trigger.

    Edge case: pe spike-through (pretul sare peste TP fara fill volume),
    limit-ul sit pana cand pretul revine. Mitigare = `tpLimitPrice` mai
    prost decat `takeProfit`. Pe TF mari (>= 30m) si TP-uri ample (>= 2×ATR)
    edge case-ul e foarte rar.

    NU folosi tpOrderType=Limit fara tpLimitPrice — Bybit returneaza eroare
    sau plaseaza limit-ul exact la trigger (maker incert daca pretul nu
    continua sa miste in directia ta).
    """
    await _post("/v5/position/set-trading-stop", {
        "category":    _cat(),
        "symbol":      symbol,
        "stopLoss":    _fmt_price(sl_price),
        "slTriggerBy": "LastPrice",
        "positionIdx": 0,
    })


# ============================================================================
# Maker entry helper — Limit PostOnly cu fallback Market pe remainder
# ============================================================================
#
# Bybit V5 nu are "chase order" nativ (verificat in /v5/order/create endpoint).
# Pentru maker fee la entry, foloseste pattern-ul "try maker once, fallback
# Market pe ce a ramas dupa timeout" — mult mai simplu decat un chase complet
# (~25 linii vs 200+) si captureaza ~80-90% din economia de fee.
#
# Detalii bug-uri evitate:
#   1. NU folosim get_position_qty pt detectare fill — fragil cu pyramiding
#      (pozitia preexistenta poate face check-ul fals-pozitiv).
#      In schimb interogam orderStatus din /v5/order/realtime.
#   2. La timeout, market doar pe `qty - cumExecQty`, nu pe qty intreg —
#      altfel double-fill garantat la partial.
#   3. Pe PostOnly rejection (piata s-a miscat in fereastra de plasare),
#      place_limit_postonly returneaza None instant → fallback Market imediat,
#      nu astepta timeout-ul degeaba.


async def maker_entry_or_market(symbol:      str,
                                side:        str,           # "Buy" / "Sell"
                                qty:         float,
                                top:         Optional[dict] = None,
                                timeout_sec: int   = 5,
                                fallback:    str   = "market",   # "market" | "skip"
                                min_qty:     float = 0.0,
                                reduce_only: bool  = False) -> dict:
    """
    Entry MAKER cu fallback configurabil pe remainder. Pattern 80/20 — captureaza
    ~80-90% din economia de fee fata de un chase complet, ~50 linii.

    Pasi:
      1. Plaseaza Limit PostOnly la best bid (Buy) / best ask (Sell).
         Daca PostOnly e respins instant (piata s-a miscat) -> fallback imediat.
      2. Astepta `timeout_sec` x 1s. Verifica orderStatus dupa fiecare secunda.
         Daca orderStatus == "Filled" -> succes ca maker.
      3. Timeout -> cancel ordinul. Verifica `cumExecQty`.
         - fallback="market": Market pe REMAINDER (anti-double-fill).
         - fallback="skip":   nu mai trimite Market — accepti underfill total
                              (sau partial-fill maker daca a fost partial).

    REGULA MENTALA pt alegerea fallback:
        - Daca pierderea de a NU intra/iesi < costul taker  -> fallback="skip"
        - Daca pierderea de a NU intra/iesi >= costul taker -> fallback="market"
        - Orice exit de PROTECTIE (SL/trail/BE) -> NU folosi pattern-ul,
          place_market direct (siguranta executiei > economia de fee).

    GHID timeout_sec + fallback per scenariu:

        ENTRIES                                 timeout  fallback
          - Breakout / volatil                    3s     market
          - Mean reversion / calm                 5-7s   market

        EXITS PROFIT                            timeout  fallback
          - TP final (close all, ai timp)        10s     market
          - TP partial (scale-out, runner ramane) 15-20s skip sau market
            (daca pierzi TP partial, runner-ul preia profitul oricum)

        ADAOSURI POZITIE                        timeout  fallback
          - Pyramidare (optionala prin definitie) 5s     skip
            (taker-ul anuleaza economia + adauga slippage; mai bine ratezi
             adaugarea decat sa fortezi fill cu cost)

        NU FOLOSI PATTERN-UL (Market direct, fara helper):
          - Stop loss
          - Trailing stop
          - Break-even stop
          - Orice exit de protecție / risk management

    Args:
      top:         {"bid","ask"} sau None -> se face REST get_ticker intern.
      timeout_sec: vezi ghid de mai sus.
      fallback:    "market" sau "skip". Daca "skip", la timeout nu se plaseaza
                   Market — strategia primeste cum_qty real si decide ea.
      min_qty:     prag minim sub care nu se trimite Market la fallback="market"
                   (qty step — eviti reject-uri pe ordere prea mici).
      reduce_only: True pt EXIT-uri (TP, scale-out). False pt ENTRY/pyramidare.

    Returneaza:
      {
        "result":     "maker"   - filled 100% maker
                      "taker"   - rejection imediata SAU 100% market fallback
                      "mixed"   - partial maker + market remainder
                      "skipped" - timeout cu fallback="skip" (filled_qty 0 sau partial)
                      "failed"  - place_market a esuat (caz rar; logat),
        "filled_qty":   float,    # cantitate reala fillata (poate fi < qty pe skip)
        "avg_price":    float,    # avg maker din Bybit; pe mixed/taker e estimativ
      }
    """
    # Top of book — REST fallback daca nu primim de la caller
    if top is None:
        t = await get_ticker(symbol)
        top = {"bid": t["bid1"], "ask": t["ask1"]} if t else {}
    px = top.get("bid") if side == "Buy" else top.get("ask")
    if not px:
        # Niciun top -> direct Market (sau skip)
        if fallback == "skip":
            return {"result": "skipped", "filled_qty": 0.0, "avg_price": 0.0}
        market_id = await place_market(symbol, side, qty, reduce_only=reduce_only)
        return {"result": "taker" if market_id else "failed",
                "filled_qty": qty if market_id else 0.0,
                "avg_price":  0.0}

    # 1. Plasare maker. None -> rejection PostOnly sau alt error.
    oid = await place_limit_postonly(symbol, side, px, qty,
                                     reduce_only=reduce_only)
    if not oid:
        # Bug fix #3: NU astepta timeout. Fallback imediat (sau skip).
        if fallback == "skip":
            return {"result": "skipped", "filled_qty": 0.0, "avg_price": 0.0}
        market_id = await place_market(symbol, side, qty, reduce_only=reduce_only)
        return {"result": "taker" if market_id else "failed",
                "filled_qty": qty if market_id else 0.0,
                "avg_price":  0.0}

    # 2. Poll order status (NU position qty — bug fix #1, evita probleme cu pyramiding)
    for _ in range(timeout_sec):
        await asyncio.sleep(1)
        st = await get_order_status(symbol, oid)
        if st and st.get("orderStatus") == "Filled":
            return {"result": "maker",
                    "filled_qty": float(st.get("cumExecQty", qty) or qty),
                    "avg_price":  float(st.get("avgPrice",   px) or px)}

    # 3. Timeout — cancel + verifica cumExecQty (bug fix #2: market doar pe remainder)
    await cancel_order(symbol, oid)
    final = await get_order_status(symbol, oid)
    cum_qty   = float(final.get("cumExecQty", 0) or 0) if final else 0.0
    avg_maker = float(final.get("avgPrice",   0) or 0) if final else 0.0
    remaining = max(qty - cum_qty, 0.0)

    if fallback == "skip":
        # Strategia accepta underfill — returnam ce am obtinut maker (poate fi 0)
        return {"result": "skipped",
                "filled_qty": cum_qty,
                "avg_price":  avg_maker}

    # fallback == "market": completeaza pe remainder
    if remaining > min_qty:
        await place_market(symbol, side, remaining, reduce_only=reduce_only)

    if cum_qty > 0:
        return {"result": "mixed",
                "filled_qty": qty,         # presupunem market a fillat restul
                "avg_price":  avg_maker}   # avg afisat e cel maker; slippage real
                                            # vine cand fetch_pnl_for_trade trage
                                            # avg-ul ponderat de pe Bybit
    return {"result": "taker",
            "filled_qty": qty,
            "avg_price":  0.0}


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
