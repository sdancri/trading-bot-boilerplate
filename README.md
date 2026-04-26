# Trading Bot Boilerplate

**Schelet reutilizabil pentru boturi de trading algoritmist pe Bybit V5.**
Producție-grade: asyncio single-thread, WS+REST sync, rate-limited, order
tracking, chart live în browser (Lightweight Charts), notificări Telegram,
containerizat pentru Portainer.

```
     ┌──────────┐    WS kline     ┌─────────────┐   WS /ws    ┌──────────┐
     │  Bybit   │────────────────▶│             │◀───────────▶│ Browser  │
     │ Exchange │    REST         │  FastAPI    │   live      │ Chart    │
     │          │◀───────────────▶│  + asyncio  │             │          │
     │          │    WS privat    │  + strategy │             └──────────┘
     │          │────────────────▶│  (Python)   │   Bot API   ┌──────────┐
     └──────────┘                 └─────┬───────┘────────────▶│ Telegram │
                                        │                      └──────────┘
                                        │ live state
                                        ▼
                                 state.account
                                 (100 + Σ PnL real)
```

---

## Ce face boilerplate-ul pentru tine

| Concern trading | Rezolvat prin |
|---|---|
| Plasare ordine pe Bybit V5 | `core.exchange_api.place_market/stop_limit/limit_postonly` |
| Exit maker-fee (chase post-only) | `core.exchange_api.chase_close` |
| PnL real per trade (fees incluse) | `core.exchange_api.fetch_pnl_for_trade` |
| Equity-ul bot-ului = 100 + Σ PnL real | `bot_state.BotState.add_closed_trade` |
| Position sizing utilities (notional = risk × 100 / SL%) | `position_sizing.py` (fără leverage în calcul) |
| Limite SL% min/max | `strategies.base_strategy.validate_sl` |
| Warmup indicatori pe istoric, fără lookahead | `strategy.load_history()` + `no_lookahead.filter_closed_bars` |
| Sync REST → WS la pornire (fără gap, fără duplicat) | `server._sync_anchor_rest` + `_fetch_gap_bars` |
| Tracking fill/partial/rejected | `core/private_ws.py` + `strategy.on_order_event` |
| Rate limit protection (fără IP ban) | `rate_limiter.py` — token bucket global |
| Reconectare automată crash/network | WS tasks cu loop + retry 5s |
| Chart live în browser (TZ Bucharest) | `static/chart_live.html` — Lightweight Charts |
| Indicatori overlay pe chart (EMA, VWAP) | `ctx.push_indicator(name, ts, value)` |
| Chart afișează DOAR bare de la prima pornire | `state.first_candle_ts` + filter client-side |
| Notificări Telegram cu nume bot | `telegram_bot._header()` cu `BOT_NAME` env |
| Deployment Docker + Portainer | `Dockerfile` + `docker-compose.yml` |

---

## Quick start

### 1. Clonare + setup local

```bash
git clone https://github.com/sdancri/trading-bot-boilerplate.git
cd trading-bot-boilerplate
chmod +x scripts/*.sh
cp .env.example .env
# Editează .env: BOT_NAME, SYMBOL, Bybit API keys, Telegram token
```

### 2. Run local (pentru testare)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

Deschide `http://localhost:8090/` în browser — vezi chart-ul live.

### 3. Run via Docker (recomandat)

```bash
docker compose up -d
docker compose logs -f bot
```

### 4. Deploy pe VPS cu Portainer

Vezi [`ARCHITECTURE.md`](ARCHITECTURE.md#deployment-pe-vps-cu-portainer) pentru
ghid complet (Stack from Git vs Pre-built image, reverse proxy SSL, loguri).

---

## Cum scrii o strategie nouă

Creezi o clasă care moștenește `Strategy` și implementează hook-urile:

```python
from strategies.base_strategy import Strategy, StrategyContext, validate_sl
import core.exchange_api as ex


class MyStrategy(Strategy):

    def __init__(self, symbol: str) -> None:
        super().__init__(
            name="my_strategy_v1",
            symbol=symbol,
            interval="5",              # 5m candles
            history_bars=300,          # câte bare la warmup
        )
        self._in_trade = False

    async def on_start(self, ctx: StrategyContext) -> None:
        # Înregistrează indicatori pentru chart (overlay peste price)
        ctx.register_indicator("MyEMA", color="#ffd700", line_width=2)

        # Warmup intern pe istoricul deja filtrat (no lookahead)
        for bar in self.history:
            self._update_my_indicator(bar["close"])

        await ctx.send_telegram(
            f"STRATEGY READY — {self.name}",
            f"Warmup pe {len(self.history)} bare istorice"
        )

    async def on_candle(self, ctx: StrategyContext, candle: dict) -> None:
        ts, c, confirmed = candle["ts"], candle["close"], candle["confirmed"]

        if not confirmed:
            # OK pentru SL/TP check cu high/low, NU pt logica de entry
            return

        # Update indicator + publicare pe chart (valoare deja MATURĂ)
        self._update_my_indicator(c)
        await ctx.push_indicator("MyEMA", ts, self._my_ema)

        # Logica strategie
        if self._signal() and not self._in_trade:
            await self._open_trade(ctx, ts, c)

    async def on_order_event(self, ctx, event_type, data) -> None:
        # Reset state dacă order-ul a fost respins
        if event_type == "order" and data.get("orderStatus") == "Rejected":
            self._in_trade = False
            await ctx.send_telegram("⚠️ Order rejected",
                                    data.get("rejectReason", "?"))

    async def _open_trade(self, ctx, ts, entry):
        sl = entry * 0.997   # decizia ta: SL% per trade
        ok, sl_pct, reason = validate_sl(entry, sl)
        if not ok:
            print(f"  SKIP — {reason}")
            return

        # Sizing — e decizia TA ce balance folosești și ce procent de risc:
        #   ctx.state.initial_account  → capital fix de pornire (constant)
        #   ctx.state.account          → equity curent (se schimbă cu PnL)
        # Alege în funcție de profilul de risc al strategiei tale.
        snap = ex.sizing_snapshot(
            balance=ctx.state.initial_account,     # sau ctx.state.account
            risk_frac=0.05,                         # 5% (exemplu arbitrar)
            entry_price=entry,
            sl_price=sl,
        )

        order_id = await ex.place_market(self.symbol, "Buy", snap["qty"])
        if order_id:
            await ex.set_position_sl(self.symbol, sl)
            self._in_trade = True
            # ... trackează entry_ts, entry_price, etc.
```

Apoi în `.env` setezi `STRATEGY_MODULE` și `STRATEGY_CLASS` la calea de
import și numele clasei tale (formatul Python `package.module`):
```
STRATEGY_MODULE=<calea.modulului.tau>
STRATEGY_CLASS=<NumeleClaseiTale>
```

Restart:
```bash
docker compose restart bot
```

---

## Variabile de mediu (toate în `.env.example`)

| Variabilă | Default | Descriere |
|---|---|---|
| `BOT_NAME` | `my_strategy_v1` | Apare în header Telegram + chart title |
| `SYMBOL` | `BTCUSDT` | Instrumentul tranzacționat |
| `STRATEGY_MODULE` | `strategies.base_strategy` | Modul Python al strategiei |
| `STRATEGY_CLASS` | `NoopStrategy` | Clasa din modul |
| `CHART_PORT` | `8090` | Port HTTP pentru chart |
| `CHART_TZ` | `Europe/Bucharest` | Fusul orar afișat |
| `ACCOUNT_SIZE` | `100.0` | Capital inițial pentru equity (reset la restart) |
| `SL_MIN_PCT` | `0.15` | Reject trade dacă SL% < |
| `SL_MAX_PCT` | `1.50` | Reject trade dacă SL% > |
| `BYBIT_API_KEY` | — | Cheie Bybit (obligatoriu pt trading) |
| `BYBIT_API_SECRET` | — | Secret Bybit (obligatoriu pt trading) |
| `BYBIT_TESTNET` | `0` | `1` pentru testnet |
| `QTY_STEP` | `0.001` | Lot size minim (BTCUSDT perp) |
| `QTY_PRECISION` | `3` | Zecimale pentru qty |
| `PRICE_PRECISION` | `2` | Zecimale pentru price |
| `WS_KLINE_INTERVAL` | `5` | Bybit interval: `1`, `3`, `5`, `15`, `60`, `240`, `D` |
| `RATE_LIMIT_PER_SEC` | `5` | Token bucket rate |
| `RATE_LIMIT_BURST` | `10` | Token bucket burst |
| `TELEGRAM_TOKEN` | — | Bot token |
| `TELEGRAM_CHAT_ID` | — | ID-ul chat-ului unde trimite mesajele |

---

## Structura

```
.
├── main.py                        # entry point — FastAPI + dirijor bot
│
├── core/                          # ⛔ NU modifica aici cand adaugi o strategie
│   ├── __init__.py
│   ├── exchange_api.py            # Bybit V5 REST (orders, PnL, klines)
│   ├── private_ws.py              # WS privat (order/execution/position)
│   ├── bot_state.py               # state.account + trades + equity + indicators
│   ├── rate_limiter.py            # token bucket protection
│   ├── position_sizing.py         # notional = risk × 100 / SL% (zero leverage)
│   ├── no_lookahead.py            # anti-lookahead utilities
│   └── telegram_bot.py            # notificări cu BOT_NAME
│
├── strategies/
│   ├── __init__.py
│   └── base_strategy.py           # ⭐ CONTRACT — Strategy ABC + NoopStrategy placeholder
│
├── static/
│   └── chart_live.html            # frontend (Lightweight Charts + WS)
│
├── chart_template.py              # (separat) template chart static pt backtest
├── Dockerfile
├── docker-compose.yml
├── requirements.txt               # core dependencies
├── requirements-extra.txt         # pandas-ta, sklearn (opțional)
├── scripts/
│   ├── build_push_docker.sh       # push DockerHub (sdancri)
│   └── push_github.sh             # push GitHub (sdancri)
├── .github/workflows/
│   └── docker-publish.yml         # CI/CD DockerHub automat
├── .env.example
├── .dockerignore
├── .gitignore
├── ARCHITECTURE.md                # concurrency + Portainer + order tracking
├── LICENSE
└── README.md                      # acest fișier
```

## Contractul strategiilor

Regula de aur:

> - Framework-ul e în `core/` + `main.py`; nu e gândit să fie modificat.
> - Strategia ta moștenește `Strategy` (din `strategies.base_strategy`) și
>   implementează `on_start` și `on_candle` (obligatorii), `on_order_event`
>   (recomandat), `on_trade_closed` (opțional).
> - La rulare, framework-ul importă clasa prin `STRATEGY_MODULE` și
>   `STRATEGY_CLASS` din `.env` — calea modulului tău e la alegerea ta.
> - Specificația completă + semnaturile hook-urilor: vezi `strategies/base_strategy.py`.

---

## FAQ

**Î: Unde dispar datele după restart?**
R: Intenționat. `BotState()` ține totul în RAM. Restart = istoric trades gol,
`state.account = ACCOUNT_SIZE` (default $100). Dacă vrei persistence,
adaugă pickle/JSON în `BotState.__init__/add_closed_trade`.

**Î: Ce se întâmplă dacă bot-ul crashează cu un trade deschis?**
R: Restart → strategia nu știe că era în trade. Fix opțional: în `on_start()`,
apelează `ex.get_position_qty(symbol)` și reconstruiește state-ul dacă > 0.
Vezi `ARCHITECTURE.md` pentru exemplu complet.

**Î: Cum afișez RSI/MACD (în pane separat sub chart)?**
R: Momentan chart-ul suportă doar overlay pe price (EMA, VWAP, BB).
Pentru indicatori pe sub, adaugă al doilea `chart.addLineSeries` cu
`priceScaleId: "rsi"` și setup custom scale în `chart_live.html`.

**Î: Pot rula 2 boturi simultan pe același VPS?**
R: Da — fiecare container cu propriul `CHART_PORT` (ex 8090 vs 8091) și
propriul `BOT_NAME`. Clonezi repo-ul în 2 foldere, configurezi `.env`
diferite, pornești amândouă cu `docker compose up -d`.

**Î: De ce există `get_balance()` în `core/exchange_api.py` dacă nu-l folosesc?**
R: Pentru debugging — verificare manuală a driftului dintre `state.account`
local și balance-ul real Bybit. NU folosi pentru equity/sizing — bot-ul
trebuie să fie single source of truth.

**Î: Cum testez logica fără să risc bani?**
R: `BYBIT_TESTNET=1` în `.env`. Folosește [testnet.bybit.com](https://testnet.bybit.com).

**Î: Cum simulez strategia pe istoric (backtest)?**
R: Boilerplate-ul e focusat pe LIVE. Pentru backtest folosește iteratorul
`no_lookahead.iter_bars_no_lookahead()` cu datele tale CSV:
```python
for bar_i, past_bars in nl.iter_bars_no_lookahead(hist, min_warmup=50):
    ema = compute_ema([b["close"] for b in past_bars], 50)
    # ... semnal + execuție simulată
```
Pentru rapoarte HTML, vezi `chart_template.py` (deja inclus).

---

## Licență

MIT — vezi `LICENSE`. Folosește cum vrei, fără garanții.

**⚠️ Trading-ul crypto cu leverage e risc mare. Boilerplate-ul ăsta e doar
infrastructura — strategia e responsabilitatea ta. Testează pe testnet
înainte să pui capital real.**

---

## ⚠️ Warning: dacă înlocuiești `httpx` cu `ccxt`

Boilerplate-ul folosește `httpx` cu calls directe la endpoint-urile Bybit V5
(`/v5/market/instruments-info`, `/v5/order/create`, etc.) cu `category=linear`
explicit la fiecare request. Asta e RAPID și PREDICTIBIL.

**Dacă forkuiești și înlocuiești cu `ccxt.bybit`** (mai high-level dar mai opaque),
ai grijă la următoarele capcane care pot face bot-ul să **crash în loop la
startup pe VPS-uri cu network slow**:

### 1. `load_markets()` încarcă TOATE categoriile

ccxt apelează implicit `fetch_markets` pentru `spot + linear + inverse + option`.
Endpoint-ul `option` are mii de strike-uri (BTC/ETH/SOL/etc.) și e foarte lent.

**Fix**: limitează la categoria pe care o folosești efectiv:
```python
ex = ccxt.bybit({...})
ex.options['fetchMarkets'] = ['linear']   # doar USDT perpetuals
```

### 2. `fetch_currencies()` lovește endpoint privat

ccxt `load_markets()` apelează implicit `fetch_currencies()` care lovește
`/v5/asset/coin/query-info` — endpoint **PRIVAT** ce cere scope `Wallet/Asset Read`
pe API key. Dacă API key-ul e Trade-only (cum ar trebui), apelul timeout-ează.

**Fix**: dezactivează complet:
```python
ex.has['fetchCurrencies'] = False
```

### 3. Default timeout 10s prea strict pe VPS slow

**Fix**:
```python
ex = ccxt.bybit({"timeout": 30_000, ...})
```

### 4. Setup crash → Docker restart loop → file descriptor leak

Dacă bot crash la setup() (din motive de mai sus), `restart: unless-stopped` din
docker-compose va încerca din nou imediat → loop infinit. Fiecare crash lasă
`Unclosed client session` (aiohttp) → FD leak → CPU spike pe `epoll_wait`.

**Fix preventiv**:
```python
client = await BybitClient.create(...)
try:
    await runner.setup()
except Exception:
    await client.close()   # asigură cleanup pe crash
    raise
```

Plus în compose:
```yaml
deploy:
  restart_policy:
    condition: on-failure
    max_attempts: 5
    window: 60s
```

### Recomandare

**Stay on `httpx` direct** dacă strategia ta nu cere features avansate ccxt
(unified API across exchanges, etc.). E mai puțin magic, mai rapid, mai ușor
de debug. Boilerplate-ul ăsta e proof că funcționează în production.
