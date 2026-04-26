"""
no_lookahead.py — Utilitati anti-lookahead
============================================
Lookahead = folosirea de date care nu ar fi fost disponibile la momentul
cand strategia ia decizia. In teste, asta face backtest-ul sa arate mult
mai bine decat realitatea. Reguli stricte:

1. BARA CURENTA IN CURS e INTERZISA pentru decizii.
   Ultimul kline primit de la Bybit e posibil sa fie bara curenta (in curs).
   Pentru indicatori si semnale, foloseste doar bare INCHISE.

2. LA TF SUPERIOR (ex EMA 4H intr-o strategie 5m), bara 4H care contine
   bara 5m curenta e INCOMPLETA. Foloseste bara 4H ANTERIOARA (ultima inchisa).

3. IN on_candle, daca `confirmed=False`, bara curenta inca se formeaza:
   - OK sa folosesti high/low pt a detecta SL/TP hit (piata a trecut deja
     prin acele nivele — asta NU e lookahead)
   - NU e OK sa folosesti close-ul pt calcule de indicator sau pt generarea
     de semnale de entry (close-ul inca se poate misca)

4. IN backtest, itereaza bar-by-bar cronologic. La barul i, ai voie sa folosesti
   DOAR barele [0..i-1] (inchise). Barul i e pentru execution (entry/exit
   pe open, sau verificare SL/TP cu high/low), NU pentru decizii pe close.
"""
from __future__ import annotations

import time


# Durata unei bare Bybit in ms
_INTERVAL_MS = {
    "1":   60_000,
    "3":   180_000,
    "5":   300_000,
    "15":  900_000,
    "30":  1_800_000,
    "60":  3_600_000,
    "120": 7_200_000,
    "240": 14_400_000,     # 4H
    "360": 21_600_000,
    "720": 43_200_000,
    "D":   86_400_000,
    "W":   604_800_000,
}


def interval_ms(interval: str) -> int:
    """Bybit interval ('5', '60', 'D', ...) -> milisecunde."""
    ms = _INTERVAL_MS.get(interval)
    if ms is None:
        raise ValueError(f"Unknown interval: {interval}")
    return ms


def current_bar_open_ms(now_ms: int, interval: str) -> int:
    """
    Ora deschiderii barei curente (cea IN CURS).

    Ex: now=14:37:23 pe TF 5m -> 14:35:00 (bara 14:35-14:39)
    """
    imms = interval_ms(interval)
    return (now_ms // imms) * imms


def last_closed_bar_open_ms(now_ms: int, interval: str) -> int:
    """
    Ora deschiderii ULTIMEI BARE INCHISE (ultima care ar fi fost disponibila).

    Ex: now=14:37:23 pe TF 5m -> 14:30:00 (bara 14:30-14:34 e inchisa la 14:35)
    """
    return current_bar_open_ms(now_ms, interval) - interval_ms(interval)


def filter_closed_bars(bars: list[dict], interval: str,
                       now_ms: int | None = None,
                       ts_key: str = "ts") -> list[dict]:
    """
    Elimina din lista toate barele a caror ts >= bara curenta deschisa.
    (adica elimina bara in curs + orice bar din viitor).

    bars: list de dict cu field `ts` in ms UTC (open time a barei).
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    cutoff = current_bar_open_ms(now_ms, interval)
    return [b for b in bars if b[ts_key] < cutoff]


def align_higher_tf(lower_bar_ts_ms: int, higher_tf: str) -> int:
    """
    Pt bara `lower_bar_ts_ms` de pe TF mic, returneaza ts-ul ULTIMEI BARE
    INCHISE de pe TF mare.

    Previne lookahead pe resample.

    Ex BUG-ul clasic: la 14:37 pe 5m, daca folosesti EMA(4H) calculat pe
    seria care include bara 4H curenta (12:00-16:00, inca in curs), ai
    lookahead — acea EMA se poate schimba pana la 16:00.

    FIX: foloseste bara 4H anterioara (08:00-12:00, deja inchisa la 12:00).

    Args:
        lower_bar_ts_ms: ts-ul barei curente de pe TF mic (in ms)
        higher_tf:       "60" / "240" / "D" etc.

    Returns:
        ts_ms al ULTIMEI BARE INCHISE de pe TF superior.
    """
    ms = interval_ms(higher_tf)
    # Inceputul barei TF-superior care CONTINE bara inferioara curenta
    current_higher_open = (lower_bar_ts_ms // ms) * ms
    # Returnam bara ANTERIOARA acelei bare (deja inchise)
    return current_higher_open - ms


# ============================================================================
# Backtest iterator helper (opțional — pt cine vrea sa scrie teste)
# ============================================================================

def iter_bars_no_lookahead(bars: list[dict], min_warmup: int = 0):
    """
    Iterator bar-by-bar, cronologic, fara lookahead.

    Pentru fiecare bar `i >= min_warmup`:
        yield (current_bar, past_bars)
    unde `past_bars` sunt TOATE barele [0..i-1] (INCHISE, fara barul curent).

    Strategia poate:
      - Folosi `past_bars` pt indicatori si semnale (inchise, no lookahead)
      - Folosi `current_bar` DOAR pt:
          * execution (entry/exit la open-ul barului i)
          * verificare SL/TP hit cu high/low (asta e corect — piata a
            trecut prin acele nivele)

    Typical pattern:
        for bar_i, past in iter_bars_no_lookahead(hist, min_warmup=50):
            ema = compute_ema([b["close"] for b in past], 50)
            if signal_from(ema, past):
                execute_trade(entry=bar_i["open"])   # open-ul lui bar_i OK

    Args:
        bars:       list[dict] ASC ordonat (oldest->newest)
        min_warmup: sarim primele N bare (nu ai destule date pt indicator)
    """
    for i in range(min_warmup, len(bars)):
        yield bars[i], bars[:i]
