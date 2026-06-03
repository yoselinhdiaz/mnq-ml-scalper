"""
dashboard/server.py
Lightweight HTTP server that serves the dashboard UI and state.json.
Run with: python dashboard/server.py
Or starts automatically alongside main.py via --dashboard flag.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8765
STATE_PATH = "logs/state.json"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_html()
        elif self.path == "/state":
            self._serve_state()
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_state(self):
        try:
            with open(STATE_PATH) as f:
                data = f.read()
        except FileNotFoundError:
            data = json.dumps({"error": "No state yet — bot not running"})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data.encode())

    def _serve_html(self):
        html = _DASHBOARD_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, *_):
        pass   # suppress server request logs


def start_background(port: int = PORT):
    """Start server in a daemon thread (called from main.py)."""
    server = HTTPServer(("", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def main():
    print(f"Dashboard -> http://localhost:{PORT}")
    server = HTTPServer(("", PORT), Handler)
    server.serve_forever()


# ------------------------------------------------------------------ #
#  Dashboard HTML (single-file, no external deps)                     #
# ------------------------------------------------------------------ #

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>US100 ML Scalper</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500&display=swap');

  :root {
    --bg:      #0b0e12;
    --bg2:     #111418;
    --bg3:     #181c22;
    --border:  #1e2530;
    --text:    #cdd6e0;
    --muted:   #4a5568;
    --green:   #00d97e;
    --red:     #ff4d6a;
    --amber:   #f5a623;
    --blue:    #4da6ff;
    --mono:    'IBM Plex Mono', monospace;
    --sans:    'IBM Plex Sans', sans-serif;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: var(--mono);
         font-size: 13px; min-height: 100vh; }

  header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 24px; border-bottom: 1px solid var(--border);
    background: var(--bg2);
  }
  .logo { font-size: 15px; font-weight: 600; letter-spacing: .04em; color: #fff; }
  .logo span { color: var(--blue); }
  .badge {
    font-size: 11px; padding: 3px 10px; border-radius: 4px; font-weight: 500;
    letter-spacing: .06em;
  }
  .badge.paper  { background: rgba(245,166,35,.12); color: var(--amber); border: 1px solid rgba(245,166,35,.25); }
  .badge.live   { background: rgba(0,217,126,.12);  color: var(--green); border: 1px solid rgba(0,217,126,.25);
                  animation: pulse 2s infinite; }
  .badge.off    { background: rgba(74,85,104,.15);  color: var(--muted); border: 1px solid var(--border); }
  .badge.gpu    { background: rgba(77,166,255,.12); color: var(--blue);  border: 1px solid rgba(77,166,255,.25); }
  .badge.cpu    { background: rgba(74,85,104,.15);  color: var(--muted); border: 1px solid var(--border); }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.6} }

  .ts { font-size: 11px; color: var(--muted); font-family: var(--sans); }

  main { padding: 20px 24px; display: flex; flex-direction: column; gap: 16px; }

  /* Metric row */
  .metrics { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
  .card {
    background: var(--bg2); border: 1px solid var(--border); border-radius: 8px;
    padding: 14px 16px;
  }
  .card .label { font-size: 10px; color: var(--muted); letter-spacing: .08em;
                 text-transform: uppercase; margin-bottom: 6px; font-family: var(--sans); }
  .card .value { font-size: 22px; font-weight: 600; line-height: 1; }
  .card .sub   { font-size: 11px; color: var(--muted); margin-top: 4px; font-family: var(--sans); }
  .green { color: var(--green); }
  .red   { color: var(--red); }
  .amber { color: var(--amber); }
  .blue  { color: var(--blue); }

  /* Signal strip */
  .signal-strip {
    background: var(--bg2); border: 1px solid var(--border); border-radius: 8px;
    padding: 14px 18px; display: flex; align-items: center; gap: 24px;
  }
  .sig-label { font-size: 11px; color: var(--muted); letter-spacing: .06em;
               text-transform: uppercase; font-family: var(--sans); }
  .sig-value { font-size: 28px; font-weight: 600; letter-spacing: .02em; }
  .sig-meta  { font-size: 12px; color: var(--muted); font-family: var(--sans); }
  .divider   { width: 1px; height: 40px; background: var(--border); }
  .prob-bar-wrap { flex: 1; }
  .prob-bar-bg { height: 4px; background: var(--bg3); border-radius: 2px; overflow: hidden; }
  .prob-bar-fill { height: 100%; border-radius: 2px; transition: width .4s ease; }

  /* Two-col layout */
  .cols { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }

  /* Trade table */
  .trade-table { width: 100%; border-collapse: collapse; }
  .trade-table th {
    text-align: left; font-size: 10px; color: var(--muted); padding: 0 8px 8px;
    letter-spacing: .07em; text-transform: uppercase; font-family: var(--sans);
    border-bottom: 1px solid var(--border);
  }
  .trade-table td { padding: 7px 8px; border-bottom: 1px solid rgba(30,37,48,.6); }
  .trade-table tr:last-child td { border-bottom: none; }
  .pnl-pos { color: var(--green); }
  .pnl-neg { color: var(--red); }

  /* Mini bars — signal distribution */
  .dist-row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .dist-name { width: 48px; font-size: 11px; }
  .dist-bar-bg { flex: 1; height: 6px; background: var(--bg3); border-radius: 3px; overflow: hidden; }
  .dist-bar-fill { height: 100%; border-radius: 3px; transition: width .5s ease; }
  .dist-count { width: 36px; text-align: right; font-size: 11px; color: var(--muted); }

  /* Open trade banner */
  .open-banner {
    border-radius: 8px; padding: 12px 18px;
    display: flex; align-items: center; gap: 16px;
    border: 1px solid;
  }
  .open-banner.long  { background: rgba(0,217,126,.06);  border-color: rgba(0,217,126,.2); }
  .open-banner.short { background: rgba(255,77,106,.06); border-color: rgba(255,77,106,.2); }

  #update-dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--muted); display: inline-block;
    transition: background .2s;
  }
  #update-dot.flash { background: var(--green); }

  .no-data { color: var(--muted); font-family: var(--sans); font-size: 13px;
             padding: 20px 0; text-align: center; }
</style>
</head>
<body>

<header>
  <div style="display:flex;align-items:center;gap:12px">
    <div class="logo">US<span>100</span> · ML Scalper</div>
    <span id="mode-badge" class="badge off">OFFLINE</span>
    <span id="device-badge" class="badge cpu">CPU</span>
  </div>
  <div style="display:flex;align-items:center;gap:10px">
    <span id="update-dot"></span>
    <span id="last-update" class="ts">--</span>
  </div>
</header>

<main>

  <!-- Metrics -->
  <div class="metrics">
    <div class="card">
      <div class="label">Paper P&amp;L</div>
      <div class="value" id="equity">$0.00</div>
      <div class="sub" id="dd-sub">DD: $0.00</div>
    </div>
    <div class="card">
      <div class="label">Win Rate</div>
      <div class="value" id="winrate">--</div>
      <div class="sub" id="trades-sub">0 trades</div>
    </div>
    <div class="card">
      <div class="label">ATR</div>
      <div class="value blue" id="atr">--</div>
      <div class="sub" id="chop-sub">chop --</div>
    </div>
    <div class="card">
      <div class="label">Price</div>
      <div class="value" id="price">--</div>
      <div class="sub" id="bar-sub">bar --</div>
    </div>
  </div>

  <!-- Signal strip -->
  <div class="signal-strip">
    <div>
      <div class="sig-label">Signal</div>
      <div class="sig-value" id="signal-val">--</div>
    </div>
    <div class="divider"></div>
    <div style="flex:1">
      <div style="display:flex;justify-content:space-between;margin-bottom:6px">
        <span class="sig-meta">Confidence</span>
        <span class="sig-meta" id="prob-pct">--%</span>
      </div>
      <div class="prob-bar-bg">
        <div class="prob-bar-fill" id="prob-bar" style="width:0%;background:var(--muted)"></div>
      </div>
      <div style="display:flex;justify-content:space-between;margin-top:4px">
        <span class="ts">0%</span>
        <span class="ts" style="color:var(--amber)">threshold 62%</span>
        <span class="ts">100%</span>
      </div>
    </div>
  </div>

  <!-- Open trade (if any) -->
  <div id="open-wrap"></div>

  <!-- Two columns -->
  <div class="cols">

    <!-- Trade history -->
    <div class="card">
      <div class="label" style="margin-bottom:12px">Trade history</div>
      <div id="trade-list"><div class="no-data">No trades yet</div></div>
    </div>

    <!-- Signal distribution -->
    <div class="card">
      <div class="label" style="margin-bottom:14px">Signal distribution</div>
      <div id="dist"></div>

      <div style="margin-top:20px">
        <div class="label" style="margin-bottom:10px">Bars processed</div>
        <div style="font-size:28px;font-weight:600" id="bars-count">0</div>
      </div>
    </div>

  </div>

</main>

<script>
const $ = id => document.getElementById(id);

function fmt(n, dec=2) {
  return n == null ? '--' : Number(n).toFixed(dec);
}

function colorClass(n) {
  return n > 0 ? 'green' : n < 0 ? 'red' : '';
}

function render(s) {
  // Mode badge
  const badge = $('mode-badge');
  if (s.paper) {
    badge.textContent = 'PAPER'; badge.className = 'badge paper';
  } else {
    badge.textContent = 'LIVE'; badge.className = 'badge live';
  }

  // Device badge
  const dev = (s.device || 'cpu').toLowerCase();
  const devBadge = $('device-badge');
  if (dev === 'cuda') {
    devBadge.textContent = 'GPU'; devBadge.className = 'badge gpu';
  } else {
    devBadge.textContent = 'CPU'; devBadge.className = 'badge cpu';
  }

  // Last update
  $('last-update').textContent = new Date().toLocaleTimeString();
  const dot = $('update-dot');
  dot.classList.add('flash');
  setTimeout(() => dot.classList.remove('flash'), 300);

  // Equity
  const eq = s.equity || 0;
  const dd = s.drawdown || 0;
  $('equity').textContent = (eq >= 0 ? '+' : '') + '$' + fmt(eq);
  $('equity').className = 'value ' + colorClass(eq);
  $('dd-sub').textContent = 'DD: $' + fmt(dd);
  $('dd-sub').style.color = dd < 0 ? 'var(--red)' : 'var(--muted)';

  // Win rate
  if (s.win_rate != null) {
    $('winrate').textContent = s.win_rate + '%';
    $('winrate').className = 'value ' + (s.win_rate >= 50 ? 'green' : 'red');
  }
  $('trades-sub').textContent = (s.total_trades || 0) + ' trades';

  // ATR / chop
  $('atr').textContent = fmt(s.atr);
  const chop = s.chop || 0;
  $('chop-sub').textContent = 'chop ' + fmt(chop, 3);
  $('chop-sub').style.color = chop > 0.7 ? 'var(--red)' : chop > 0.5 ? 'var(--amber)' : 'var(--green)';

  // Price
  $('price').textContent = fmt(s.price, 1);
  $('bar-sub').textContent = 'bar ' + (s.bar_time || '--').slice(11, 16);

  // Signal
  const sig = s.signal || 'SKIP';
  const sigEl = $('signal-val');
  sigEl.textContent = sig;
  sigEl.className = 'sig-value ' + (sig === 'LONG' ? 'green' : sig === 'SHORT' ? 'red' : 'amber');

  // Prob bar
  const prob = (s.prob || 0) * 100;
  $('prob-pct').textContent = fmt(prob, 1) + '%';
  const bar = $('prob-bar');
  bar.style.width = prob + '%';
  bar.style.background = prob >= 62
    ? (sig === 'LONG' ? 'var(--green)' : sig === 'SHORT' ? 'var(--red)' : 'var(--amber)')
    : 'var(--muted)';

  // Open trade banner
  const wrap = $('open-wrap');
  if (s.open_trade) {
    const t = s.open_trade;
    const cls = t.direction === 'LONG' ? 'long' : 'short';
    const color = t.direction === 'LONG' ? 'var(--green)' : 'var(--red)';
    const pnl = t.pnl || 0;
    wrap.innerHTML = `
      <div class="open-banner ${cls}">
        <div>
          <div class="sig-label">Open trade</div>
          <div class="sig-value" style="color:${color};font-size:20px">${t.direction}</div>
        </div>
        <div class="divider"></div>
        <div><div class="ts">Entry</div><div style="font-size:15px;font-weight:500">${fmt(t.entry,1)}</div></div>
        <div><div class="ts">SL</div><div style="font-size:15px;color:var(--red)">${fmt(t.entry - (t.direction==='LONG'?t.sl:-t.sl), 1)}</div></div>
        <div><div class="ts">TP</div><div style="font-size:15px;color:var(--green)">${fmt(t.entry + (t.direction==='LONG'?t.tp:-t.tp), 1)}</div></div>
        <div style="margin-left:auto">
          <div class="ts">Float P&L</div>
          <div style="font-size:18px;font-weight:600" class="${colorClass(pnl)}">${pnl >= 0 ? '+' : ''}$${fmt(pnl)}</div>
        </div>
      </div>`;
  } else {
    wrap.innerHTML = '';
  }

  // Trade history table
  const trades = (s.trades || []).slice().reverse();
  const listEl = $('trade-list');
  if (trades.length === 0) {
    listEl.innerHTML = '<div class="no-data">No trades yet</div>';
  } else {
    listEl.innerHTML = `
      <table class="trade-table">
        <thead><tr>
          <th>Time</th><th>Dir</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Reason</th>
        </tr></thead>
        <tbody>
          ${trades.slice(0,15).map(t => `
            <tr>
              <td class="ts">${(t.time||'').slice(11,16)}</td>
              <td style="color:${t.direction==='LONG'?'var(--green)':'var(--red)'}">${t.direction}</td>
              <td>${fmt(t.entry,1)}</td>
              <td>${fmt(t.exit,1)}</td>
              <td class="${t.pnl>=0?'pnl-pos':'pnl-neg'}">${t.pnl>=0?'+':''}$${fmt(t.pnl)}</td>
              <td class="ts">${t.reason||''}</td>
            </tr>`).join('')}
        </tbody>
      </table>`;
  }

  // Signal distribution
  const sigs = s.signals || {LONG:0, SHORT:0, SKIP:0};
  const total = (sigs.LONG||0) + (sigs.SHORT||0) + (sigs.SKIP||0) || 1;
  const distEl = $('dist');
  distEl.innerHTML = [
    {name:'LONG',  color:'var(--green)', count: sigs.LONG  || 0},
    {name:'SHORT', color:'var(--red)',   count: sigs.SHORT || 0},
    {name:'SKIP',  color:'var(--muted)', count: sigs.SKIP  || 0},
  ].map(({name, color, count}) => `
    <div class="dist-row">
      <span class="dist-name" style="color:${color}">${name}</span>
      <div class="dist-bar-bg">
        <div class="dist-bar-fill" style="width:${(count/total*100).toFixed(1)}%;background:${color}"></div>
      </div>
      <span class="dist-count">${count}</span>
    </div>`).join('');

  $('bars-count').textContent = s.bars_seen || 0;
}

async function poll() {
  try {
    const r = await fetch('/state');
    if (r.ok) render(await r.json());
  } catch(e) {}
  setTimeout(poll, 2000);
}

poll();
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
