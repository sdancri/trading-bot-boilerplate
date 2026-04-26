"""
rate_limiter.py — Token bucket pentru REST requests
=====================================================
Bybit V5 request limits (valide la 2026):
  - Market data (public, unsigned):  120 req/5s per IP    = 24 req/s
  - Order management (signed):        10 req/s per UID
  - Position endpoints (signed):      10 req/s per UID

Penalitate la depasire: rate limit error (retCode 10006) si, daca persista,
IP ban temporar. Pe VPS partajat sau retele mobile, un ban te poate lasa
offline zile intregi — deci limiterul e non-optional.

Cum functioneaza token bucket:
  - La pornire, bucket-ul are `burst` tokens.
  - Se regenereaza cu `rate_per_sec` tokens/sec, pana la max `burst`.
  - Fiecare request consuma 1 token. Daca nu sunt tokens, request-ul asteapta.

Defaults conservative (5 req/s, burst 10) — lasa margine pt WS ping-uri si
pentru alte requests care ar putea fi in flight. Override via env:
    RATE_LIMIT_PER_SEC=10
    RATE_LIMIT_BURST=20
"""
from __future__ import annotations

import asyncio
import os
import time


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: int) -> None:
        self.rate  = float(rate_per_sec)
        self.burst = int(burst)
        self.tokens: float = float(burst)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """
        Asteapta pana e un token disponibil, il consuma, returneaza secundele
        de asteptare (0 daca nu s-a asteptat).
        """
        async with self._lock:
            waited = 0.0
            while True:
                now     = time.monotonic()
                elapsed = now - self._last
                self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
                self._last  = now

                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return waited

                # Insufficient tokens -> wait for 1 to regenerate
                need = 1.0 - self.tokens
                wait = need / self.rate
                waited += wait
                await asyncio.sleep(wait)


# Global singleton — toate call-urile REST trec prin el
_bucket = TokenBucket(
    rate_per_sec=float(os.getenv("RATE_LIMIT_PER_SEC", "5")),
    burst=int(os.getenv("RATE_LIMIT_BURST", "10")),
)


async def wait_token() -> None:
    """
    Apelat inainte de FIECARE request REST.
    Daca prea multe requests in flight, blocheaza non-blocant (async sleep)
    pana cand bucket-ul se umple.
    """
    waited = await _bucket.acquire()
    if waited > 0.1:
        print(f"  [RATE] Throttled {waited*1000:.0f}ms pt a proteja rate limits")
