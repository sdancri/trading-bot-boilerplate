"""
chart_template.py
=================
Template reusabil pentru vizualizarea backtesturilor pe Lightweight Charts
(TradingView). Genereaza un fisier HTML standalone cu:
  - Candlestick chart cu Entry markers (sageti + marime pozitie)
  - Exit markers: checkmark albastru (TP) / X gri (SL)
  - SL/TP/Entry price lines la click pe trade
  - Tooltip cu data, zi in romana, ora (UTC+3 Bucuresti)
  - Tabel lateral cu: Data, Directie, PnL
  - Total PnL in footer

Utilizare
---------
    from chart_template import ChartDisplay

    # `df`     = DataFrame cu coloanele: datetime, open, high, low, close
    # `trades` = lista de obiecte Trade sau dicts
    cd = ChartDisplay(df, trades, title="Strategy 30m ETH/USDT")
    cd.save("backtest_chart.html")   # genereaza fisierul
    cd.open()                        # il deschide in browser

Campuri asteptate per trade (dict sau obiect cu atribute):
    entry_time   : pd.Timestamp sau datetime
    exit_time    : pd.Timestamp sau datetime
    side         : "L" sau "S"
    entry_price  : float
    sl           : float
    tp           : float
    qty          : float
    pnl          : float
    exit_reason  : "TP" | "SL"
"""
from __future__ import annotations

import json
import math
import webbrowser
from pathlib import Path
from typing import Union
import pandas as pd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_DAYS_RO = ["Luni", "Marti", "Miercuri", "Joi", "Vineri", "Sambata", "Duminica"]

def _ts_ms(ts) -> int:
    """Timestamp -> milisecunde UTC (Lightweight Charts asteapta ms UTC)."""
    if hasattr(ts, "value"):          # np.datetime64
        return int(ts.value) // 1_000_000
    return int(pd.Timestamp(ts).timestamp() * 1000)

def _fmt_dt(ts, tz="Europe/Bucharest") -> str:
    """Formateaza timestamp pentru afisare: 'Luni, 14.04.2025  08:30'."""
    t = pd.Timestamp(ts).tz_localize("UTC") if pd.Timestamp(ts).tzinfo is None \
        else pd.Timestamp(ts)
    t = t.tz_convert(tz)
    day_ro = _DAYS_RO[t.weekday()]
    return f"{day_ro}, {t.strftime('%d.%m.%Y  %H:%M')}"


# ---------------------------------------------------------------------------
# ChartDisplay
# ---------------------------------------------------------------------------
class ChartDisplay:
    """
    Parameters
    ----------
    df : pd.DataFrame
        OHLCV cu index sau coloana 'datetime'.
    trades : list
        Lista de Trade (obiecte sau dicts).
    title : str
        Titlul ferestrei HTML.
    tz : str
        Timezone pentru afisare (default Europe/Bucharest = UTC+3/UTC+2 DST).
    initial_capital : float
        Capitalul initial pentru calculul PnL%.
    """
    def __init__(self, df: pd.DataFrame, trades: list,
                 title: str = "Backtest Chart",
                 tz: str = "Europe/Bucharest",
                 initial_capital: float = 100.0):
        self.df = df.copy()
        self.trades = trades
        self.title = title
        self.tz = tz
        self.initial_capital = initial_capital
        self._prepare_df()
        self.price_precision = self._detect_price_precision()
        self.price_min_move  = 10 ** (-self.price_precision)

    # -- precizie pret auto (~5 cifre semnificative) -------------------------
    def _detect_price_precision(self) -> int:
        try:
            cols = [c for c in ("open", "high", "low", "close") if c in self.df.columns]
            if not cols:
                return 4
            prices = pd.concat([self.df[c] for c in cols]).astype(float)
            prices = prices[prices > 0]
            if prices.empty:
                return 4
            ref = float(prices.min())
            if not math.isfinite(ref) or ref <= 0:
                return 4
            magnitude = math.floor(math.log10(ref))
            return max(2, min(8, 4 - magnitude))
        except Exception:
            return 4

    # -- normalizare DataFrame -----------------------------------------------
    def _prepare_df(self):
        df = self.df
        if "datetime" in df.columns:
            df = df.set_index("datetime")
        df.index = pd.to_datetime(df.index)
        df.columns = [c.lower() for c in df.columns]
        self.df = df

    # -- helper acces trade field --------------------------------------------
    @staticmethod
    def _tf(trade, field, default=None):
        if isinstance(trade, dict):
            return trade.get(field, default)
        return getattr(trade, field, default)

    # -- serializa date chart ------------------------------------------------
    def _candles_json(self) -> str:
        p = self.price_precision
        rows = []
        for ts, row in self.df.iterrows():
            day_ro = _DAYS_RO[pd.Timestamp(ts).weekday()]
            try:
                t_local = pd.Timestamp(ts).tz_localize("UTC").tz_convert(self.tz)
                tooltip = f"{day_ro}, {t_local.strftime('%d.%m.%Y  %H:%M')}"
            except Exception:
                tooltip = str(ts)
            rows.append({
                "time":    _ts_ms(ts),
                "open":    round(float(row["open"]),  p),
                "high":    round(float(row["high"]),  p),
                "low":     round(float(row["low"]),   p),
                "close":   round(float(row["close"]), p),
                "tooltip": tooltip,
            })
        return json.dumps(rows)

    # -- serializa traduri ---------------------------------------------------
    def _trades_json(self) -> str:
        result = []
        cum_pnl = 0.0
        for i, t in enumerate(self.trades):
            entry_time  = self._tf(t, "entry_time")
            exit_time   = self._tf(t, "exit_time")
            side        = self._tf(t, "side", "L")
            entry_price = float(self._tf(t, "entry_price", 0))
            sl          = float(self._tf(t, "sl",          0))
            tp          = float(self._tf(t, "tp",          0))
            qty         = float(self._tf(t, "qty",         0))
            pnl         = float(self._tf(t, "pnl",         0))
            exit_reason = self._tf(t, "exit_reason", "")
            cum_pnl += pnl
            size_usdt   = qty * entry_price
            pnl_pct     = pnl / self.initial_capital * 100

            p = self.price_precision
            result.append({
                "id":          i + 1,
                "entry_ms":    _ts_ms(entry_time),
                "exit_ms":     _ts_ms(exit_time) if exit_time else None,
                "side":        side,
                "entry_price": round(entry_price, p),
                "sl":          round(sl, p),
                "tp":          round(tp, p),
                "size_usdt":   round(size_usdt, 2),
                "pnl":         round(pnl, 4),
                "pnl_pct":     round(pnl_pct, 4),
                "cum_pnl":     round(cum_pnl, 4),
                "exit_reason": exit_reason,
                "entry_label": _fmt_dt(entry_time, self.tz),
            })
        return json.dumps(result)

    # -- genereaza HTML ------------------------------------------------------
    def render(self) -> str:
        candles_json = self._candles_json()
        trades_json  = self._trades_json()
        init_cap     = self.initial_capital
        precision    = self.price_precision
        min_move_str = f"{self.price_min_move:.{precision}f}"

        html = f"""<!DOCTYPE html>
<html lang="ro">
<head>
<meta charset="UTF-8">
<title>{self.title}</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
  :root {{
    --bg:        #0b0e17;
    --surface:   #0f141f;
    --border:    #1a2535;
    --text:      #c8d8e8;
    --dim:       #556677;
    --green:     #00e676;
    --red:       #ff3352;
    --yellow:    #ffd700;
    --blue:      #3399ff;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'Consolas', 'JetBrains Mono', monospace;
    font-size: 12px;
    display: flex;
    height: 100vh;
    overflow: hidden;
  }}

  /* Chart wrapper */
  #chart-wrapper {{
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }}
  #chart-title {{
    padding: 6px 12px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    font-size: 13px;
    color: var(--yellow);
    letter-spacing: .04em;
    font-weight: 600;
    flex-shrink: 0;
  }}
  #wrap-chart {{ flex: 1; position: relative; min-height: 0; }}
  #chart {{ width: 100%; height: 100%; }}

  /* Tooltip hover */
  #tooltip {{
    position: absolute;
    top: 36px; left: 8px;
    background: rgba(15,20,31,.92);
    border: 1px solid var(--border);
    padding: 6px 10px;
    pointer-events: none;
    display: none;
    z-index: 50;
    line-height: 1.6;
    border-radius: 6px;
    min-width: 180px;
  }}
  #tooltip .tt-date  {{ color: var(--yellow); font-weight: bold; }}
  #tooltip .tt-ohlc  {{ color: var(--dim); margin-top: 2px; }}
  #tooltip .tt-val   {{ color: var(--text); }}

  /* Panel lateral */
  #side-panel {{
    width: 290px;
    flex-shrink: 0;
    background: var(--surface);
    border-left: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }}
  #panel-header {{
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    color: var(--blue);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: .06em;
    text-transform: uppercase;
    flex-shrink: 0;
  }}
  #trades-list {{
    flex: 1 1 auto;
    overflow-y: auto;
    scrollbar-width: thin;
    scrollbar-color: var(--border) var(--surface);
  }}
  #trades-list::-webkit-scrollbar {{ width: 5px; }}
  #trades-list::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 3px; }}

  table.trade-table {{
    width: 100%;
    border-collapse: collapse;
  }}
  table.trade-table thead th {{
    position: sticky; top: 0;
    background: #0d1420;
    color: var(--dim);
    padding: 5px 7px;
    font-weight: normal;
    text-align: right;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }}
  table.trade-table thead th:first-child {{ text-align: left; }}
  table.trade-table tbody tr {{
    cursor: pointer;
    transition: background .15s;
  }}
  table.trade-table tbody tr:hover {{ background: rgba(255,255,255,.04); }}
  table.trade-table tbody tr.selected {{ background: rgba(51,153,255,.12); }}
  table.trade-table td {{
    padding: 4px 7px;
    text-align: right;
    border-bottom: 1px solid rgba(26,37,53,.6);
    white-space: nowrap;
  }}
  table.trade-table td:first-child {{ text-align: left; }}
  .win  {{ color: var(--green); }}
  .loss {{ color: var(--red);   }}
  .dir-l {{ color: var(--green); font-weight: bold; }}
  .dir-s {{ color: var(--red);   font-weight: bold; }}

  #total-row {{
    flex-shrink: 0;
    padding: 8px 12px;
    border-top: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: #0d1420;
  }}
  #total-row .lbl {{ color: var(--dim); }}
  #total-row .val {{ font-size: 13px; font-weight: bold; }}
</style>
</head>
<body>

<div id="chart-wrapper">
  <div id="chart-title">{self.title}</div>
  <div id="wrap-chart">
    <div id="chart"></div>
    <div id="tooltip"></div>
  </div>
</div>

<div id="side-panel">
  <div id="panel-header">LISTA TRADURI</div>
  <div id="trades-list">
    <table class="trade-table" id="tbl">
      <thead>
        <tr>
          <th>Data</th>
          <th>Dir</th>
          <th>PnL $</th>
        </tr>
      </thead>
      <tbody id="tbl-body"></tbody>
    </table>
  </div>
  <div id="total-row">
    <span class="lbl">TOTAL (<span id="total-count">0</span> traduri)</span>
    <span class="val" id="total-pnl">-</span>
  </div>
</div>

<script>
const CANDLES = {candles_json};
const TRADES  = {trades_json};
const INIT_CAP = {init_cap};
const PRICE_PRECISION = {precision};
const PRICE_MIN_MOVE  = {min_move_str};

// -- Lightweight Charts ----------------------------------------------------
const wrapChart = document.getElementById('wrap-chart');
const chartEl   = document.getElementById('chart');
const chart = LightweightCharts.createChart(chartEl, {{
  width:  chartEl.clientWidth,
  height: chartEl.clientHeight,
  layout: {{
    background: {{ color: '#0b0e17' }},
    textColor:  '#556677',
  }},
  grid: {{
    vertLines:  {{ color: '#1a2535' }},
    horzLines:  {{ color: '#1a2535' }},
  }},
  crosshair: {{
    mode: LightweightCharts.CrosshairMode.Normal,
  }},
  timeScale: {{
    timeVisible:    true,
    secondsVisible: false,
    borderColor:    '#1a2535',
    rightOffset:     10,
    barSpacing:      6,
  }},
  rightPriceScale: {{
    borderColor: '#1a2535',
  }},
}});

// Resize: save range, resize, restore
const _ro = new ResizeObserver(() => {{
  const r = chart.timeScale().getVisibleRange();
  chart.resize(chartEl.clientWidth, wrapChart.clientHeight);
  if (r) requestAnimationFrame(() => chart.timeScale().setVisibleRange(r));
}});
_ro.observe(wrapChart);

// Candlestick series
const candleSeries = chart.addCandlestickSeries({{
  upColor:        '#00e676',
  downColor:      '#ff3352',
  borderUpColor:  '#00e676',
  borderDownColor:'#ff3352',
  wickUpColor:    '#00e676',
  wickDownColor:  '#ff3352',
  priceFormat: {{
    type:      'price',
    precision: PRICE_PRECISION,
    minMove:   PRICE_MIN_MOVE,
  }},
}});
const chartData = CANDLES.map(c => ({{
  time: c.time / 1000, open: c.open, high: c.high, low: c.low, close: c.close
}}));
candleSeries.setData(chartData);

// Markers (entry + exit)
const entryMarkers = TRADES.map(t => ({{
  time:      t.entry_ms / 1000,
  position:  t.side === 'L' ? 'belowBar' : 'aboveBar',
  color:     t.side === 'L' ? '#00e676'  : '#ff3352',
  shape:     t.side === 'L' ? 'arrowUp'  : 'arrowDown',
  text:      t.side + ' $' + t.size_usdt.toFixed(0),
  size:      2,
}}));
const exitMarkers = TRADES.filter(t => t.exit_ms).map(t => ({{
  time:      t.exit_ms / 1000,
  position:  t.exit_reason === 'TP' ? 'aboveBar' : 'belowBar',
  color:     t.exit_reason === 'TP' ? '#3399ff'  : '#888888',
  shape:     'circle',
  text:      t.exit_reason === 'TP' ? '\\u2714' : '\\u2718',
  size:      2,
}}));
const markers = [...entryMarkers, ...exitMarkers].sort((a, b) => a.time - b.time);
candleSeries.setMarkers(markers);

// SL/TP/Entry price lines (on trade click)
let slLine = null, tpLine = null, entryLine = null;

function clearTradeLines() {{
  if (slLine)    {{ candleSeries.removePriceLine(slLine);    slLine = null; }}
  if (tpLine)    {{ candleSeries.removePriceLine(tpLine);    tpLine = null; }}
  if (entryLine) {{ candleSeries.removePriceLine(entryLine); entryLine = null; }}
}}

function showTradeLines(trade) {{
  clearTradeLines();
  entryLine = candleSeries.createPriceLine({{
    price: trade.entry_price, color: '#ffd700',
    lineWidth: 1, lineStyle: 2, axisLabelVisible: true,
    title: 'Entry #' + trade.id + '  ' + trade.size_usdt.toFixed(0) + '$',
  }});
  slLine = candleSeries.createPriceLine({{
    price: trade.sl, color: '#ff3352',
    lineWidth: 1, lineStyle: 2, axisLabelVisible: true,
    title: 'SL',
  }});
  tpLine = candleSeries.createPriceLine({{
    price: trade.tp, color: '#00e676',
    lineWidth: 1, lineStyle: 2, axisLabelVisible: true,
    title: 'TP',
  }});
}}

// -- Tooltip ---------------------------------------------------------------
const tooltipEl = document.getElementById('tooltip');
const candleMap = {{}};
CANDLES.forEach(c => {{ candleMap[c.time] = c; }});

chart.subscribeCrosshairMove(param => {{
  if (!param.time || !param.seriesData) {{
    tooltipEl.style.display = 'none';
    return;
  }}
  const ms  = param.time * 1000;
  const bar = candleMap[ms];
  if (!bar) {{ tooltipEl.style.display = 'none'; return; }}
  const d   = param.seriesData.get(candleSeries);
  if (!d)   {{ tooltipEl.style.display = 'none'; return; }}

  tooltipEl.innerHTML =
    '<div class="tt-date">' + bar.tooltip + '</div>' +
    '<div class="tt-ohlc">' +
      'O <span class="tt-val">' + d.open.toFixed(PRICE_PRECISION)  + '</span>  ' +
      'H <span class="tt-val" style="color:#00e676">' + d.high.toFixed(PRICE_PRECISION)  + '</span>  ' +
      'L <span class="tt-val" style="color:#ff3352">' + d.low.toFixed(PRICE_PRECISION)   + '</span>  ' +
      'C <span class="tt-val">' + d.close.toFixed(PRICE_PRECISION) + '</span>' +
    '</div>';
  tooltipEl.style.display = 'block';
}});

// -- Tabel traduri ---------------------------------------------------------
const tbody = document.getElementById('tbl-body');
let selectedRow = null;

TRADES.forEach(t => {{
  const pnlClass = t.pnl >= 0 ? 'win' : 'loss';
  const dirClass = t.side === 'L' ? 'dir-l' : 'dir-s';
  const pnlSign  = t.pnl >= 0 ? '+' : '';
  const exMark   = t.exit_reason === 'TP' ? ' \\u2713' : (t.exit_reason === 'SL' ? ' \\u2717' : '');
  const tr = document.createElement('tr');
  tr.dataset.id = t.id;
  tr.innerHTML =
    '<td style="font-size:11px;color:#8899aa">' + t.entry_label + '</td>' +
    '<td class="' + dirClass + '">' + t.side + '</td>' +
    '<td class="' + pnlClass + '">' + pnlSign + t.pnl.toFixed(2) + exMark + '</td>';
  tr.addEventListener('click', () => {{
    if (selectedRow) selectedRow.classList.remove('selected');
    tr.classList.add('selected');
    selectedRow = tr;
    showTradeLines(t);
    chart.timeScale().setVisibleRange({{
      from: (t.entry_ms / 1000) - 86400 * 3,
      to:   (t.exit_ms  ? t.exit_ms / 1000 : t.entry_ms / 1000) + 86400 * 3,
    }});
  }});
  tbody.appendChild(tr);
}});

// Total PnL
if (TRADES.length > 0) {{
  const lastCum = TRADES[TRADES.length - 1].cum_pnl;
  const totPct  = lastCum / INIT_CAP * 100;
  const sign    = lastCum >= 0 ? '+' : '';
  const col     = lastCum >= 0 ? '#00e676' : '#ff3352';
  document.getElementById('total-count').textContent = TRADES.length;
  document.getElementById('total-pnl').style.color = col;
  document.getElementById('total-pnl').textContent =
    sign + lastCum.toFixed(2) + '$  (' + sign + totPct.toFixed(2) + '%)';
}}

chart.timeScale().scrollToRealTime();
</script>
</body>
</html>"""
        return html

    # -- salvare fisier ------------------------------------------------------
    def save(self, path: Union[str, Path] = "backtest_chart.html") -> Path:
        p = Path(path)
        p.write_text(self.render(), encoding="utf-8")
        print(f"Chart salvat -> {p.resolve()}")
        return p

    # -- deschide in browser -------------------------------------------------
    def open(self, path: Union[str, Path] = "backtest_chart.html") -> None:
        p = self.save(path)
        webbrowser.open(p.resolve().as_uri())
