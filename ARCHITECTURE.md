## Deployment pe VPS cu Portainer

### Setup-ul arhitectural
Când Portainer e deja instalat pe VPS-ul tău și vrei să deploy-ezi bot-ul:

**Opțiunea A — Stack din repo GitHub (recomandat):**
1. Portainer → Stacks → Add stack → Git repository
2. Repository URL: `https://github.com/sdancri/trading-bot-boilerplate`
3. Compose path: `docker-compose.yml`
4. Environment variables: lipește conținutul din `.env.example` completat
5. Deploy → bot-ul pornește; chart pe `http://vps-ip:8090/`

**Opțiunea B — Imagine pre-built de pe DockerHub:**
1. Portainer → Stacks → Add stack → Web editor
2. Lipește un docker-compose minimal:
   ```yaml
   services:
     bot:
       image: sdancri/trading-bot-boilerplate:latest
       env_file: stack.env
       ports: ["8090:8090"]
       restart: unless-stopped
   ```
3. Environment variables: adaugă tot din `.env.example`
4. Deploy

### Expunere externă
Dacă vrei să accesezi chart-ul din afară (nu doar de pe VPS):
- **Firewall**: deschide portul (`ufw allow 8090/tcp`)
- **Reverse proxy cu SSL** (recomandat — nu expui direct HTTP): configurează
  Caddy/Traefik/Nginx → `chart.your-domain.com` → `localhost:8090`
- **Auth**: chart-ul e read-only dar oricine cu URL poate vedea. Adaugă
  HTTP Basic Auth în reverse proxy dacă vrei să-l protejezi.

### Loguri
`docker logs <container>` sau Portainer → Container → Logs. Prefix-uri
pentru grep rapid:
- `[WS]`      — kline public stream
- `[WS-PRIV]` — stream privat (orders)
- `[SYNC]`    — anchor/gap-fill/duplicate
- `[ORDER]`   — schimbări status ordin
- `[EXEC]`    — fills individuale
- `[POS]`     — update pozitie
- `[STATE]`   — trade închis + equity update
- `[RATE]`    — throttle (atenție dacă apar multe)
- `[STRATEGY]` — erori din hooks
- `[TG]`      — Telegram (erori sau not configured)

---

# Arhitectura — Model de concurrency

## De ce asyncio single-thread și nu multi-threading?

### Problema clasică
Librăriile grafice Python (Matplotlib, Tkinter, Qt) sunt **blocante**. Dacă
desenezi chart-ul în thread-ul principal, bot-ul se oprește între refresh-uri
și pierde tick-uri / tranzacții. Soluția tipică: un thread separat pentru
randare grafic, alt thread pentru strategie + date.

### Soluția boilerplate-ului: **chart-ul NU e în Python**

```
┌────────────────────────────────────────────────────────────────────────┐
│ PROCES PYTHON (single thread, asyncio event loop)                      │
│                                                                        │
│  ┌──────────────────────┐  ┌──────────────────────┐                    │
│  │ _bybit_ws_task       │  │ _core.private_ws    │                    │
│  │ (public kline)       │  │ (order/exec/position)│                    │
│  └──────────┬───────────┘  └──────────┬───────────┘                    │
│             │                         │                                │
│             ▼                         ▼                                │
│  ┌──────────────────────────────────────────┐  ┌───────────────────┐   │
│  │ strategy.on_candle / on_order_event      │  │ REST calls        │   │
│  │ (logica trading, indicatori)             │  │ (rate-limited)    │   │
│  └──────────────────┬───────────────────────┘  └───────────────────┘   │
│                     │                                                  │
│                     ▼                                                  │
│  ┌──────────────────────────────────────────┐                          │
│  │ _broadcast(msg)                          │                          │
│  │ → FastAPI WebSocket /ws → fiecare client │                          │
│  └──────────────────┬───────────────────────┘                          │
└─────────────────────┼──────────────────────────────────────────────────┘
                      │ WebSocket
                      ▼
┌────────────────────────────────────────────────────────────────────────┐
│ BROWSER (thread separat de-al meu, pe GPU)                             │
│  static/chart_live.html  +  Lightweight Charts (WebGL accelerat)       │
│                                                                        │
│  - nu blocheaza bot-ul (proces diferit!)                               │
│  - update in real-time (ms latency)                                    │
│  - poti deschide 0, 1 sau 10 browsers simultan — niciunul nu afecteaza │
│    bot-ul; daca niciunul nu e conectat, _broadcast face no-op          │
└────────────────────────────────────────────────────────────────────────┘
```

### De ce funcționează single-thread asyncio?

Toate operațiunile sunt **I/O-bound** (WS, REST, Telegram API), nu CPU-bound:
- `asyncio.sleep`, `ws.recv`, `httpx.post` — toate suspenda task-ul curent
  pentru a lasa altele să ruleze
- Event loop-ul jonglează tasks-urile cooperativ, fără overhead de thread
  switching
- Zero riscuri de race conditions pe state partajat (`_state`, `_candles`,
  `_clients`) — un singur task rulează la un moment dat

Threads ar fi **mai lenți** (GIL + context switching) pentru acest tip de workload.

### Când ai nevoie de threads / procese?

Doar dacă un hook de strategie face calcule **CPU-intensive** (ex: backtest
masiv în timpul `on_start`, re-training ML la fiecare bar). Atunci:

```python
import asyncio

async def on_candle(self, ctx, ts, o, h, l, c, confirmed):
    # Calcul usor in event loop (OK)
    self._update_ema(c)

    # Calcul HEAVY — scoate-l pe thread pool, nu blochezi event loop-ul
    if confirmed:
        score = await asyncio.to_thread(self._heavy_ml_predict, c)
        if score > 0.8:
            await self.enter_long(ctx)
```

`asyncio.to_thread()` trimite funcția sincronă pe un thread separat, iar event
loop-ul primary poate procesa WS ticks în timp ce rulează calculul.

---

## Order tracking (de ce e critic)

`core.exchange_api.place_market()` returnează un `order_id` **imediat**, înainte
ca order-ul să fie executat pe exchange. Order-ul poate fi:

| Status                | Ce înseamnă                                      | Ce face strategia greșit dacă nu asculta |
|-----------------------|--------------------------------------------------|------------------------------------------|
| `New`                 | Plasat în orderbook, așteaptă fill               | Crede că e deja în poziție                |
| `PartiallyFilled`     | Doar o parte din qty e umplut                    | Crede că are qty full                    |
| `Filled`              | Total umplut                                     | OK                                       |
| `Rejected`            | Refuzat (balance, tick size, invalid, etc.)      | Crede că e în poziție — state corupt      |
| `Cancelled`           | Anulat (de bot sau manual)                       | Nu știe că order-ul nu mai există         |

**Soluția: core/private_ws.py** subscribe la `order`, `execution`, `position`,
iar strategia implementează `on_order_event(ctx, event_type, data)` pentru a-și
sincroniza state-ul cu realitatea exchange-ului.

Exemplu minim (în strategia ta):

```python
async def on_order_event(self, ctx, event_type, data):
    if event_type == "order":
        status = data.get("orderStatus")
        if status == "Rejected":
            print(f"ORDER REJECTED: {data.get('rejectReason')}")
            self._in_trade = False     # <-- CRUCIAL: reset state
            await ctx.send_telegram("⚠️ Order rejected", data.get("rejectReason"))

    elif event_type == "execution":
        # Update qty real (poate fi mai mic decat cel cerut)
        self._actual_qty = float(data.get("execQty", 0))
```

---

## Rate limiting

Orice call REST (place/cancel order, fetch pnl, fetch kline) trece prin
`rate_limiter.wait_token()` — token bucket conservativ (5 req/s, burst 10).

Default-urile acoperă workload-ul obișnuit al bot-ului (1 trade la câteva ore).
Dacă faci o strategie cu multe orders/min, crește-le în `.env`:

```bash
RATE_LIMIT_PER_SEC=15
RATE_LIMIT_BURST=30
```

Atenție: Bybit V5 limits în ianuarie 2026 sunt **10 req/s pe endpoint signat**.
Nu crește peste 10.

---

## Reconectare automată

Toate stream-urile WS au loop infinit cu `try/except + sleep 5s`:

```python
while True:
    try:
        async with websockets.connect(URL) as ws:
            # ... logic ...
    except Exception as e:
        print(f"WS error: {e}, reconnect in 5s")
        await asyncio.sleep(5)
```

Combinat cu:
- `_sync_anchor_rest()` la bootstrap → ancoră istoric
- `_fetch_gap_bars()` la prima tick după reconnect → recuperează ce a lipsit

Bot-ul supraviețuiește la:
- Crash & restart (istoric se re-încarcă, gap se fill-uiește)
- Network outage (WS retry, gap-fill)
- Server Bybit offline (retry continuu)
- Schimbare de IP (auth retry pe privat WS)

**Singurul lucru care NU e persistent**: state-ul strategiei în memorie (dacă
bot-ul crashează în mijlocul unui trade deschis, la restart nu știe că era în
trade). Soluție — dacă devine important: strategia poate implementa
`on_start()` care verifică `get_position_qty()` pe Bybit și reconstruiește
state-ul din realitate.
