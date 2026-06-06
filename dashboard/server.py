"""
dashboard/server.py  –  Web UI + REST API for the ML scalper.

GET  /                        → SPA (Dashboard · Ajustes · Reportes · Perfil)
GET  /state                   → live bot state (JSON)
GET  /config                  → config.yaml as JSON (password masked)
POST /config                  → save config.yaml
GET  /reports/trades?period=  → trade stats from SQLite (day|week|month)
GET  /reports/training        → last training metrics from model_metrics.log
GET  /news                    → USD high-impact events (ForexFactory, 15-min cache)
GET  /profile                 → logs/profile.json
POST /profile                 → save logs/profile.json
"""

import json
import os
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen, Request

PORT         = 8765
STATE_PATH   = "logs/state.json"
CONFIG_PATH  = "config.yaml"
DB_PATH      = "logs/scalper.db"
METRICS_PATH = "logs/model_metrics.log"
PROFILE_PATH = "logs/profile.json"

_news_cache: dict = {"data": None, "ts": 0.0}
_news_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):

    # ── routing ──────────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._serve_html()
        elif path == "/state":
            self._serve_state()
        elif path == "/config":
            self._serve_config()
        elif path == "/reports/trades":
            self._serve_trade_report(qs.get("period", ["day"])[0])
        elif path == "/reports/weeks":
            self._serve_weekly_summary(
                qs.get("year",  [str(__import__("datetime").date.today().year)])[0],
                qs.get("month", [str(__import__("datetime").date.today().month)])[0],
            )
        elif path == "/reports/months":
            self._serve_monthly_summary(
                qs.get("year", [str(__import__("datetime").date.today().year)])[0]
            )
        elif path == "/reports/training":
            self._serve_training_report()
        elif path == "/news":
            self._serve_news()
        elif path == "/profile":
            self._serve_profile()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length).decode("utf-8")
        if self.path == "/config":
            self._save_config(body)
        elif self.path == "/profile":
            self._save_profile(body)
        else:
            self.send_response(404)
            self.end_headers()

    # ── low-level helpers ────────────────────────────────────────────────────
    def _ok(self, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        html = _HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def _serve_state(self):
        try:
            with open(STATE_PATH) as f:
                data = f.read()
        except FileNotFoundError:
            data = json.dumps({"error": "Bot not running"})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data.encode())

    # ── config ───────────────────────────────────────────────────────────────
    def _serve_config(self):
        try:
            import yaml
            with open(CONFIG_PATH) as f:
                cfg = yaml.safe_load(f)
            if cfg.get("mt5", {}).get("password"):
                cfg["mt5"]["password"] = "****"
            self._ok(cfg)
        except Exception as e:
            self._ok({"error": str(e)})

    def _save_config(self, body):
        try:
            import yaml
            new_cfg = json.loads(body)
            with open(CONFIG_PATH) as f:
                cur = yaml.safe_load(f)
            if new_cfg.get("mt5", {}).get("password") == "****":
                new_cfg.setdefault("mt5", {})["password"] = \
                    cur.get("mt5", {}).get("password", "")
            with open(CONFIG_PATH, "w") as f:
                yaml.dump(new_cfg, f, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)
            self._ok({"ok": True})
        except Exception as e:
            self._ok({"error": str(e)})

    # ── trade report ─────────────────────────────────────────────────────────
    def _serve_trade_report(self, period: str):
        cutoffs = {
            "day":   "date('now')",
            "week":  "date('now','-7 days')",
            "month": "date('now','-30 days')",
        }
        cutoff = cutoffs.get(period, cutoffs["day"])
        try:
            conn = sqlite3.connect(DB_PATH)
            cur  = conn.cursor()
            cur.execute(f"""
                SELECT direction,pnl,open_time,close_time,entry,exit,reason
                FROM trades
                WHERE date(open_time) >= {cutoff}
                ORDER BY open_time DESC
            """)
            rows = cur.fetchall()
            conn.close()

            trades = [dict(direction=r[0], pnl=r[1], open_time=r[2],
                           close_time=r[3], entry=r[4], exit=r[5], reason=r[6])
                      for r in rows]
            total  = len(trades)
            wins   = [t for t in trades if t["pnl"] > 0]
            losses = [t for t in trades if t["pnl"] < 0]
            self._ok({
                "period":   period,
                "total":    total,
                "win_pct":  round(len(wins)   / total * 100, 1) if total else 0,
                "loss_pct": round(len(losses) / total * 100, 1) if total else 0,
                "net_pnl":  round(sum(t["pnl"] for t in trades), 2),
                "avg_win":  round(sum(t["pnl"] for t in wins)   / len(wins),   2) if wins   else 0,
                "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0,
                "trades":   trades[:200],
            })
        except Exception as e:
            self._ok({"error": str(e)})

    # ── weekly summary ───────────────────────────────────────────────────────
    def _serve_weekly_summary(self, year: str, month: str):
        try:
            conn = sqlite3.connect(DB_PATH)
            cur  = conn.cursor()
            cur.execute("""
                SELECT pnl, CAST(strftime('%d', open_time) AS INTEGER) AS day
                FROM trades
                WHERE strftime('%Y', open_time) = ? AND strftime('%m', open_time) = ?
            """, (year, month.zfill(2)))
            rows = cur.fetchall()
            conn.close()

            weeks: dict = {}
            ranges = {1: "1–7", 2: "8–14", 3: "15–21", 4: "22–28", 5: "29+"}
            for pnl, day in rows:
                wk = min((day - 1) // 7 + 1, 5)
                if wk not in weeks:
                    weeks[wk] = {"week": wk, "range": ranges[wk],
                                 "total": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}
                weeks[wk]["total"] += 1
                if pnl > 0: weeks[wk]["wins"]   += 1
                if pnl < 0: weeks[wk]["losses"] += 1
                weeks[wk]["net_pnl"] += pnl

            result = []
            for wk in sorted(weeks):
                w = weeks[wk]
                w["win_pct"]  = round(w["wins"]   / w["total"] * 100, 1) if w["total"] else 0
                w["loss_pct"] = round(w["losses"] / w["total"] * 100, 1) if w["total"] else 0
                w["net_pnl"]  = round(w["net_pnl"], 2)
                result.append(w)

            self._ok({"year": year, "month": month, "weeks": result})
        except Exception as e:
            self._ok({"error": str(e)})

    # ── monthly summary ──────────────────────────────────────────────────────
    def _serve_monthly_summary(self, year: str):
        try:
            conn = sqlite3.connect(DB_PATH)
            cur  = conn.cursor()
            cur.execute("""
                SELECT pnl, CAST(strftime('%m', open_time) AS INTEGER) AS month
                FROM trades
                WHERE strftime('%Y', open_time) = ?
            """, (year,))
            rows = cur.fetchall()
            conn.close()

            months: dict = {}
            for pnl, m in rows:
                if m not in months:
                    months[m] = {"month": m, "total": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}
                months[m]["total"] += 1
                if pnl > 0: months[m]["wins"]   += 1
                if pnl < 0: months[m]["losses"] += 1
                months[m]["net_pnl"] += pnl

            result = []
            for m in range(1, 13):
                if m in months:
                    d = months[m]
                    d["win_pct"]  = round(d["wins"]   / d["total"] * 100, 1) if d["total"] else 0
                    d["loss_pct"] = round(d["losses"] / d["total"] * 100, 1) if d["total"] else 0
                    d["net_pnl"]  = round(d["net_pnl"], 2)
                    result.append(d)
                else:
                    result.append({"month": m, "total": 0, "wins": 0, "losses": 0,
                                   "net_pnl": 0, "win_pct": 0, "loss_pct": 0})

            self._ok({"year": year, "months": result})
        except Exception as e:
            self._ok({"error": str(e)})

    # ── training report ──────────────────────────────────────────────────────
    def _serve_training_report(self):
        try:
            if not os.path.exists(METRICS_PATH):
                self._ok({"error": "No se encontró " + METRICS_PATH})
                return
            with open(METRICS_PATH) as f:
                lines = f.readlines()

            fold_re  = re.compile(
                r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}).*?'
                r'Fold\s+(\d+)/(\d+).*?LONG f1:\s*([\d.]+).*?SHORT f1:\s*([\d.]+).*?acc:\s*([\d.]+)'
            )
            label_re = re.compile(r'Labels:\s*LONG=(\d+)\s+SHORT=(\d+)\s+SKIP=(\d+)')
            acc_re   = re.compile(r'[Ww]alk.forward avg accuracy:\s*([\d.]+)')

            last_start = max(
                (i for i, l in enumerate(lines) if 'Fold 1/' in l), default=-1
            )
            if last_start == -1:
                self._ok({"error": "No hay datos de folds en el log"})
                return

            block = lines[last_start:]
            folds, labels, avg_acc, train_date = [], None, None, None
            for line in block:
                m = fold_re.search(line)
                if m:
                    train_date = train_date or m.group(1)
                    folds.append({
                        "fold":     int(m.group(2)),
                        "long_f1":  float(m.group(4)),
                        "short_f1": float(m.group(5)),
                        "acc":      float(m.group(6)),
                    })
                m2 = label_re.search(line)
                if m2:
                    labels = {"LONG": int(m2.group(1)), "SHORT": int(m2.group(2)),
                              "SKIP": int(m2.group(3))}
                m3 = acc_re.search(line)
                if m3:
                    avg_acc = float(m3.group(1))

            self._ok({"train_date": train_date, "avg_accuracy": avg_acc,
                      "folds": folds, "labels": labels})
        except Exception as e:
            self._ok({"error": str(e)})

    # ── news guard ───────────────────────────────────────────────────────────
    def _serve_news(self):
        global _news_cache
        now = time.time()
        with _news_lock:
            if _news_cache["data"] and (now - _news_cache["ts"]) < 900:
                self._ok(_news_cache["data"])
                return
        try:
            import datetime
            req = Request(
                "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            raw    = json.loads(urlopen(req, timeout=8).read().decode())
            events = [e for e in raw
                      if e.get("country") == "USD" and e.get("impact") == "High"]

            result  = []
            now_utc = datetime.datetime.utcnow()
            for e in events:
                try:
                    ds     = f"{e.get('date','')} {e.get('time','')}"
                    dt     = datetime.datetime.strptime(ds, "%m/%d/%Y %I:%M%p")
                    dt_utc = dt + datetime.timedelta(hours=4)   # ET → UTC (EDT approx)
                    diff   = (dt_utc - now_utc).total_seconds() / 60
                    result.append({
                        "title":         e.get("title", ""),
                        "date":          e.get("date", ""),
                        "time":          e.get("time", ""),
                        "forecast":      e.get("forecast") or "",
                        "previous":      e.get("previous") or "",
                        "actual":        e.get("actual") or "",
                        "minutes_until": round(diff, 0),
                    })
                except Exception:
                    pass

            result.sort(key=lambda x: x["minutes_until"])
            data = {"events": result, "fetched_at": now_utc.isoformat()}
            with _news_lock:
                _news_cache = {"data": data, "ts": now}
            self._ok(data)
        except Exception as e:
            self._ok({"events": [], "error": str(e)})

    # ── profile ──────────────────────────────────────────────────────────────
    def _serve_profile(self):
        try:
            if os.path.exists(PROFILE_PATH):
                with open(PROFILE_PATH) as f:
                    self._ok(json.load(f))
            else:
                self._ok({"name": "", "email": "",
                           "timezone": "America/New_York", "notes": ""})
        except Exception as e:
            self._ok({"error": str(e)})

    def _save_profile(self, body):
        try:
            with open(PROFILE_PATH, "w") as f:
                json.dump(json.loads(body), f, indent=2)
            self._ok({"ok": True})
        except Exception as e:
            self._ok({"error": str(e)})

    def log_message(self, *_):
        pass


def start_background(port: int = PORT):
    """Start server in a daemon thread (called from main.py)."""
    server = HTTPServer(("", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def main():
    print(f"Dashboard -> http://localhost:{PORT}")
    HTTPServer(("", PORT), Handler).serve_forever()


# ── HTML (single-file SPA) ───────────────────────────────────────────────────
_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>US100 ML Scalper</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap');

:root {
  --bg:     #0b0e12; --bg2: #111418; --bg3: #181c22; --bg4: #1e2530;
  --border: #1e2530; --border2: #252d3a;
  --text:   #cdd6e0; --muted: #4a5568; --muted2: #64748b;
  --green:  #00d97e; --red: #ff4d6a; --amber: #f5a623; --blue: #4da6ff;
  --mono: 'IBM Plex Mono', monospace;
  --sans: 'IBM Plex Sans', sans-serif;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: var(--mono);
       font-size: 13px; min-height: 100vh; display: flex; flex-direction: column; }

/* ── Header ── */
header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 24px; border-bottom: 1px solid var(--border);
  background: var(--bg2); flex-shrink: 0; gap: 16px;
}
.logo { font-size: 14px; font-weight: 600; letter-spacing: .04em; color: #fff;
        white-space: nowrap; }
.logo span { color: var(--blue); }
.hdr-left  { display: flex; align-items: center; gap: 10px; }
.hdr-right { display: flex; align-items: center; gap: 10px; }

/* Nav */
nav { display: flex; gap: 4px; flex: 1; justify-content: center; }
.nav-btn {
  background: none; border: 1px solid transparent; color: var(--muted2);
  font-family: var(--mono); font-size: 12px; padding: 6px 14px;
  border-radius: 6px; cursor: pointer; transition: all .15s; letter-spacing: .02em;
}
.nav-btn:hover  { color: var(--text); border-color: var(--border2); background: var(--bg3); }
.nav-btn.active { color: var(--blue); border-color: rgba(77,166,255,.35);
                  background: rgba(77,166,255,.08); }

/* Badges */
.badge { font-size: 11px; padding: 3px 9px; border-radius: 4px; font-weight: 500;
         letter-spacing: .06em; }
.badge.paper  { background: rgba(245,166,35,.12); color: var(--amber); border: 1px solid rgba(245,166,35,.3); }
.badge.live   { background: rgba(0,217,126,.12);  color: var(--green); border: 1px solid rgba(0,217,126,.3);
                animation: pulse 2s infinite; }
.badge.off    { background: rgba(74,85,104,.15);  color: var(--muted); border: 1px solid var(--border); }
.badge.gpu    { background: rgba(77,166,255,.12); color: var(--blue);  border: 1px solid rgba(77,166,255,.3); }
.badge.cpu    { background: rgba(74,85,104,.15);  color: var(--muted); border: 1px solid var(--border); }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.6} }

.ts { font-size: 11px; color: var(--muted); font-family: var(--sans); }
#update-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--muted);
              display: inline-block; transition: background .2s; }
#update-dot.flash { background: var(--green); }

/* ── Pages ── */
.page { display: none; }
.page.active { display: block; }

/* ── Dashboard ── */
#page-dashboard { padding: 20px 24px; flex-direction: column; gap: 16px; }
.page.active#page-dashboard { display: flex; }
.metrics { display: grid; grid-template-columns: repeat(4,1fr); gap: 12px; }
.card {
  background: var(--bg2); border: 1px solid var(--border); border-radius: 8px;
  padding: 14px 16px;
}
.card .label { font-size: 10px; color: var(--muted); letter-spacing: .08em;
               text-transform: uppercase; margin-bottom: 6px; font-family: var(--sans); }
.card .value { font-size: 22px; font-weight: 600; line-height: 1; }
.card .sub   { font-size: 11px; color: var(--muted); margin-top: 4px; font-family: var(--sans); }
.green { color: var(--green); } .red { color: var(--red); }
.amber { color: var(--amber); } .blue { color: var(--blue); }

.signal-strip {
  background: var(--bg2); border: 1px solid var(--border); border-radius: 8px;
  padding: 14px 18px; display: flex; align-items: center; gap: 24px;
}
.sig-label { font-size: 11px; color: var(--muted); letter-spacing: .06em;
             text-transform: uppercase; font-family: var(--sans); }
.sig-value { font-size: 28px; font-weight: 600; letter-spacing: .02em; }
.sig-meta  { font-size: 12px; color: var(--muted); font-family: var(--sans); }
.divider   { width: 1px; height: 40px; background: var(--border); }
.prob-bar-bg   { height: 4px; background: var(--bg3); border-radius: 2px; overflow: hidden; }
.prob-bar-fill { height: 100%; border-radius: 2px; transition: width .4s ease; }

.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.trade-table { width: 100%; border-collapse: collapse; }
.trade-table th { text-align: left; font-size: 10px; color: var(--muted); padding: 0 8px 8px;
                  letter-spacing: .07em; text-transform: uppercase; font-family: var(--sans);
                  border-bottom: 1px solid var(--border); }
.trade-table td { padding: 7px 8px; border-bottom: 1px solid rgba(30,37,48,.6); }
.trade-table tr:last-child td { border-bottom: none; }
.pnl-pos { color: var(--green); } .pnl-neg { color: var(--red); }

.dist-row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.dist-name { width: 48px; font-size: 11px; }
.dist-bar-bg   { flex: 1; height: 6px; background: var(--bg3); border-radius: 3px; overflow: hidden; }
.dist-bar-fill { height: 100%; border-radius: 3px; transition: width .5s ease; }
.dist-count { width: 36px; text-align: right; font-size: 11px; color: var(--muted); }

.open-banner { border-radius: 8px; padding: 12px 18px;
               display: flex; align-items: center; gap: 16px; border: 1px solid; }
.open-banner.long  { background: rgba(0,217,126,.06);  border-color: rgba(0,217,126,.2); }
.open-banner.short { background: rgba(255,77,106,.06); border-color: rgba(255,77,106,.2); }
.no-data { color: var(--muted); font-family: var(--sans); font-size: 13px;
           padding: 20px 0; text-align: center; }

/* ── News widget ── */
.news-widget {
  background: var(--bg2); border: 1px solid var(--border); border-radius: 8px;
  padding: 14px 18px;
}
.news-widget .label { font-size: 10px; color: var(--muted); letter-spacing: .08em;
                      text-transform: uppercase; margin-bottom: 12px; font-family: var(--sans); }
.news-alert {
  background: rgba(255,77,106,.08); border: 1px solid rgba(255,77,106,.3);
  border-radius: 6px; padding: 8px 14px; margin-bottom: 10px;
  color: var(--red); font-size: 12px; font-weight: 600; letter-spacing: .03em;
}
.news-warn {
  background: rgba(245,166,35,.08); border: 1px solid rgba(245,166,35,.3);
  border-radius: 6px; padding: 8px 14px; margin-bottom: 10px;
  color: var(--amber); font-size: 12px; font-weight: 600; letter-spacing: .03em;
}
.news-row { display: flex; align-items: center; gap: 12px; padding: 6px 0;
            border-bottom: 1px solid var(--border); font-size: 12px; }
.news-row:last-child { border-bottom: none; }
.news-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.news-dot.hot    { background: var(--red); box-shadow: 0 0 6px rgba(255,77,106,.6); }
.news-dot.near   { background: var(--amber); }
.news-dot.future { background: var(--muted); }
.news-dot.past   { background: var(--bg4); }
.news-time  { color: var(--blue); width: 56px; flex-shrink: 0; }
.news-title { flex: 1; color: var(--text); }
.news-meta  { color: var(--muted); font-size: 11px; text-align: right; }

/* ── Settings ── */
#page-settings { padding: 24px; max-width: 860px; }
.settings-group {
  background: var(--bg2); border: 1px solid var(--border); border-radius: 8px;
  margin-bottom: 16px; overflow: hidden;
}
.group-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 18px; cursor: pointer; user-select: none;
  border-bottom: 1px solid var(--border);
}
.group-header:hover { background: var(--bg3); }
.group-title { font-size: 12px; font-weight: 600; letter-spacing: .06em;
               text-transform: uppercase; font-family: var(--sans); }
.group-chevron { color: var(--muted); font-size: 11px; transition: transform .2s; }
.group-body { padding: 18px; display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.group-body.collapsed { display: none; }

.field { display: flex; flex-direction: column; gap: 5px; }
.field.full { grid-column: 1 / -1; }
.field label { font-size: 10px; color: var(--muted); letter-spacing: .07em;
               text-transform: uppercase; font-family: var(--sans); }
.field input, .field select, .field textarea {
  background: var(--bg3); border: 1px solid var(--border2); border-radius: 5px;
  color: var(--text); font-family: var(--mono); font-size: 12px;
  padding: 7px 10px; outline: none; transition: border .15s;
}
.field input:focus, .field select:focus, .field textarea:focus {
  border-color: var(--blue);
}
.field select option { background: var(--bg3); }
.field .hint { font-size: 10px; color: var(--muted); font-family: var(--sans); }

.save-bar { display: flex; align-items: center; gap: 12px; padding: 8px 0 4px; }
.btn-primary {
  background: var(--blue); color: #000; border: none; border-radius: 6px;
  font-family: var(--mono); font-size: 12px; font-weight: 600;
  padding: 9px 22px; cursor: pointer; letter-spacing: .04em;
  transition: opacity .15s;
}
.btn-primary:hover { opacity: .85; }
.btn-secondary {
  background: var(--bg3); color: var(--text); border: 1px solid var(--border2);
  border-radius: 6px; font-family: var(--mono); font-size: 12px;
  padding: 9px 18px; cursor: pointer; transition: all .15s;
}
.btn-secondary:hover { border-color: var(--blue); color: var(--blue); }
.save-msg { font-size: 11px; font-family: var(--sans); }
.save-msg.ok  { color: var(--green); }
.save-msg.err { color: var(--red); }
.restart-note { font-size: 11px; color: var(--amber); font-family: var(--sans);
                margin-top: 6px; }

/* ── Reports ── */
#page-reports { padding: 24px; }
.tab-bar { display: flex; gap: 4px; margin-bottom: 20px; }
.tab-btn {
  background: none; border: 1px solid var(--border); color: var(--muted2);
  font-family: var(--mono); font-size: 12px; padding: 7px 18px;
  border-radius: 6px; cursor: pointer; transition: all .15s;
}
.tab-btn.active { color: var(--blue); border-color: rgba(77,166,255,.4);
                  background: rgba(77,166,255,.08); }
.tab-content { display: none; }
.tab-content.active { display: block; }

.period-btns { display: flex; gap: 8px; margin-bottom: 20px; }
.period-btn {
  display: flex; flex-direction: column; align-items: center; gap: 4px;
  background: var(--bg2); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 20px; cursor: pointer; transition: all .15s; min-width: 80px;
}
.period-btn:hover { border-color: var(--border2); background: var(--bg3); }
.period-btn.active { border-color: rgba(77,166,255,.4); background: rgba(77,166,255,.08); }
.period-btn .p-icon { font-size: 18px; }
.period-btn .p-label { font-size: 11px; color: var(--muted2); letter-spacing: .05em;
                       text-transform: uppercase; font-family: var(--sans); }

/* Period navigator (week/month) */
.period-nav-bar { display:flex; align-items:center; gap:14px; margin-bottom:18px; }
.period-nav-label { font-size:14px; font-weight:600; min-width:110px; text-align:center; }
.nav-arrow { background:var(--bg3); border:1px solid var(--border2); color:var(--text);
             font-size:16px; padding:5px 14px; border-radius:6px; cursor:pointer;
             transition:all .15s; font-family:var(--mono); line-height:1; }
.nav-arrow:hover { border-color:var(--blue); color:var(--blue); }

/* Week summary labels */
.summary-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(190px,1fr)); gap:12px; }
.summary-label { background:var(--bg2); border:1px solid var(--border); border-radius:8px;
                 padding:14px 16px; display:flex; flex-direction:column; gap:5px; }
.summary-label .sl-title { font-size:12px; font-weight:600; letter-spacing:.04em; font-family:var(--sans); }
.summary-label .sl-rates { display:flex; gap:8px; font-size:12px; font-weight:600; }
.summary-label .sl-win   { color:var(--green); }
.summary-label .sl-loss  { color:var(--red); }
.summary-label .sl-pnl   { font-size:16px; font-weight:600; margin-top:2px; }
.summary-label .sl-sub   { font-size:10px; color:var(--muted); font-family:var(--sans); }

/* Month grid labels */
.month-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }
.month-label { background:var(--bg2); border:1px solid var(--border); border-radius:8px;
               padding:14px 10px; display:flex; flex-direction:column; align-items:center;
               gap:3px; text-align:center; }
.month-label .ml-name { font-size:13px; font-weight:600; margin-bottom:2px; }
.month-label .ml-rates { font-size:11px; font-weight:600; }
.month-label .ml-win  { color:var(--green); }
.month-label .ml-loss { color:var(--red); }
.month-label .ml-pnl  { font-size:13px; font-weight:600; margin-top:3px; }
.month-label .ml-sub  { font-size:10px; color:var(--muted); font-family:var(--sans); }
.month-label.empty    { opacity:.3; }

/* Pagination */
.pagination-bar { display:flex; align-items:center; justify-content:space-between; margin-top:10px; }
.page-btns { display:flex; gap:4px; }
.page-btn { background:var(--bg3); border:1px solid var(--border); color:var(--muted2);
            font-family:var(--mono); font-size:11px; padding:4px 10px; border-radius:4px;
            cursor:pointer; transition:all .15s; }
.page-btn:hover { border-color:var(--border2); color:var(--text); }
.page-btn.cur   { border-color:rgba(77,166,255,.4); color:var(--blue); background:rgba(77,166,255,.08); }
.page-btn:disabled { opacity:.35; cursor:default; }

.report-cards { display: grid; grid-template-columns: repeat(3,1fr); gap: 12px; margin-bottom: 20px; }
.rep-card {
  background: var(--bg2); border: 1px solid var(--border); border-radius: 8px; padding: 16px;
}
.rep-card .rc-label { font-size: 10px; color: var(--muted); letter-spacing: .08em;
                      text-transform: uppercase; font-family: var(--sans); margin-bottom: 8px; }
.rep-card .rc-value { font-size: 26px; font-weight: 600; }
.rep-card .rc-sub   { font-size: 11px; color: var(--muted); margin-top: 4px; font-family: var(--sans); }

.fold-table { width: 100%; border-collapse: collapse; }
.fold-table th { text-align: left; font-size: 10px; color: var(--muted); padding: 0 10px 8px;
                 letter-spacing: .07em; text-transform: uppercase; font-family: var(--sans);
                 border-bottom: 1px solid var(--border); }
.fold-table td { padding: 8px 10px; border-bottom: 1px solid rgba(30,37,48,.6); }
.fold-table tr:last-child td { border-bottom: none; }
.label-pills { display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }
.lpill { font-size: 11px; padding: 4px 12px; border-radius: 20px; font-family: var(--sans); }
.lpill.l { background: rgba(0,217,126,.12); color: var(--green); border: 1px solid rgba(0,217,126,.25); }
.lpill.s { background: rgba(255,77,106,.12); color: var(--red);   border: 1px solid rgba(255,77,106,.25); }
.lpill.k { background: rgba(74,85,104,.15);  color: var(--muted); border: 1px solid var(--border); }

/* ── Profile ── */
#page-profile { padding: 24px; max-width: 480px; }
#page-profile .card { padding: 24px; }
#page-profile .field { margin-bottom: 14px; }
#page-profile textarea { width: 100%; min-height: 80px; resize: vertical; }
</style>
</head>
<body>

<!-- ── Header ─────────────────────────────────────────────── -->
<header>
  <div class="hdr-left">
    <div class="logo">US<span>100</span> · ML Scalper</div>
    <span id="mode-badge" class="badge off">OFFLINE</span>
    <span id="device-badge" class="badge cpu">CPU</span>
  </div>

  <nav>
    <button class="nav-btn active" onclick="showPage('dashboard')">📊 Dashboard</button>
    <button class="nav-btn" onclick="showPage('settings')">⚙️ Ajustes</button>
    <button class="nav-btn" onclick="showPage('reports')">📈 Reportes</button>
    <button class="nav-btn" onclick="showPage('profile')">👤 Perfil</button>
  </nav>

  <div class="hdr-right">
    <span id="update-dot"></span>
    <span id="last-update" class="ts">--</span>
  </div>
</header>

<!-- ══════════════════════════════════════════════════════════
     DASHBOARD
═════════════════════════════════════════════════════════════ -->
<div id="page-dashboard" class="page active">

  <div class="metrics">
    <div class="card">
      <div class="label">P&amp;L</div>
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
      <div class="label">Precio</div>
      <div class="value" id="price">--</div>
      <div class="sub" id="bar-sub">bar --</div>
    </div>
  </div>

  <div class="signal-strip">
    <div>
      <div class="sig-label">Señal</div>
      <div class="sig-value" id="signal-val">--</div>
    </div>
    <div class="divider"></div>
    <div style="flex:1">
      <div style="display:flex;justify-content:space-between;margin-bottom:6px">
        <span class="sig-meta">Confianza</span>
        <span class="sig-meta" id="prob-pct">--%</span>
      </div>
      <div class="prob-bar-bg">
        <div class="prob-bar-fill" id="prob-bar" style="width:0%;background:var(--muted)"></div>
      </div>
      <div style="display:flex;justify-content:space-between;margin-top:4px">
        <span class="ts">0%</span>
        <span class="ts" style="color:var(--amber)">umbral 62%</span>
        <span class="ts">100%</span>
      </div>
    </div>
  </div>

  <div id="open-wrap"></div>

  <!-- News widget -->
  <div class="news-widget">
    <div class="label">📰 Noticias USD — Alto Impacto</div>
    <div id="news-alert-banner"></div>
    <div id="news-list"><div class="no-data">Cargando noticias…</div></div>
  </div>

  <div class="cols">
    <div class="card">
      <div class="label" style="margin-bottom:12px">Historial de trades</div>
      <div id="trade-list"><div class="no-data">Sin trades aún</div></div>
    </div>
    <div class="card">
      <div class="label" style="margin-bottom:14px">Distribución de señales</div>
      <div id="dist"></div>
      <div style="margin-top:20px">
        <div class="label" style="margin-bottom:10px">Barras procesadas</div>
        <div style="font-size:28px;font-weight:600" id="bars-count">0</div>
      </div>
    </div>
  </div>

</div><!-- /page-dashboard -->

<!-- ══════════════════════════════════════════════════════════
     AJUSTES
═════════════════════════════════════════════════════════════ -->
<div id="page-settings" class="page">

  <!-- Cuenta MT5 -->
  <div class="settings-group">
    <div class="group-header" onclick="toggleGroup(this)">
      <span class="group-title">🖥 Cuenta MetaTrader 5</span>
      <span class="group-chevron">▼</span>
    </div>
    <div class="group-body">
      <div class="field"><label>Login (nro. cuenta)</label><input type="number" id="s-mt5-login"></div>
      <div class="field"><label>Password</label><input type="password" id="s-mt5-password" placeholder="****"></div>
      <div class="field"><label>Servidor (broker)</label><input type="text" id="s-mt5-server"></div>
      <div class="field"><label>Símbolo</label><input type="text" id="s-mt5-symbol"></div>
      <div class="field">
        <label>Timeframe principal</label>
        <select id="s-mt5-timeframe">
          <option>M1</option><option>M5</option><option>M15</option>
          <option>M30</option><option>H1</option><option>H4</option>
        </select>
      </div>
      <div class="field">
        <label>HTF Timeframe</label>
        <select id="s-mt5-htf_timeframe">
          <option>M5</option><option>M15</option><option>M30</option><option>H1</option>
        </select>
      </div>
      <div class="field"><label>Magic number</label><input type="number" id="s-mt5-magic"></div>
      <div class="field"><label>Contract size</label><input type="number" id="s-mt5-contract_size"></div>
      <div class="field full">
        <label>Ruta ejecutable MT5 (terminal_path)</label>
        <input type="text" id="s-mt5-terminal_path" placeholder="C:\\Program Files\\MetaTrader 5\\terminal64.exe">
        <span class="hint">Déjalo vacío si MT5 se detecta automáticamente</span>
      </div>
    </div>
  </div>

  <!-- Training -->
  <div class="settings-group">
    <div class="group-header" onclick="toggleGroup(this)">
      <span class="group-title">🧠 Training / Datos</span>
      <span class="group-chevron">▼</span>
    </div>
    <div class="group-body">
      <div class="field"><label>Lookback bars</label><input type="number" id="s-data-lookback_bars"><span class="hint">Barras históricas al iniciar</span></div>
      <div class="field"><label>Feature window</label><input type="number" id="s-data-feature_window"><span class="hint">Ventana rolling de features</span></div>
      <div class="field"><label>Retrain interval (horas)</label><input type="number" id="s-data-retrain_interval_hours"><span class="hint">Frecuencia de re-entrenamiento</span></div>
      <div class="field"><label>Min bars para operar</label><input type="number" id="s-data-min_bars_to_trade"></div>
    </div>
  </div>

  <!-- Estrategia -->
  <div class="settings-group">
    <div class="group-header" onclick="toggleGroup(this)">
      <span class="group-title">⚡ Estrategia / Modelo</span>
      <span class="group-chevron">▼</span>
    </div>
    <div class="group-body">
      <div class="field">
        <label>Device</label>
        <select id="s-model-device"><option>auto</option><option>cuda</option><option>cpu</option></select>
      </div>
      <div class="field"><label>Confidence threshold</label><input type="number" id="s-model-confidence_threshold" step="0.01" min="0" max="1"><span class="hint">Prob mínima para operar</span></div>
      <div class="field"><label>SR proximity %</label><input type="number" id="s-model-sr_proximity_pct" step="0.0001"><span class="hint">Bloquear cerca de S/R</span></div>
      <div class="field"><label>Counter-trend boost</label><input type="number" id="s-model-counter_trend_boost" step="0.01"><span class="hint">Confianza extra vs tendencia</span></div>
      <div class="field"><label>Flip confidence threshold</label><input type="number" id="s-model-flip_confidence_threshold" step="0.01"></div>
      <div class="field"><label>Chop ATR ratio</label><input type="number" id="s-model-chop_atr_ratio" step="0.1"></div>
      <div class="field"><label>Label lookahead (barras)</label><input type="number" id="s-model-label_lookahead"></div>
      <div class="field"><label>Label momentum bars</label><input type="number" id="s-model-label_momentum_bars"></div>
      <div class="field"><label>Label threshold ATR</label><input type="number" id="s-model-label_threshold_atr" step="0.1"></div>
    </div>
  </div>

  <!-- Risk SL / TP / BE -->
  <div class="settings-group">
    <div class="group-header" onclick="toggleGroup(this)">
      <span class="group-title">🛡 Risk · SL / TP / BE</span>
      <span class="group-chevron">▼</span>
    </div>
    <div class="group-body">
      <div class="field"><label>Riesgo por trade (USD)</label><input type="number" id="s-risk-risk_per_trade_usd"><span class="hint">Stop Loss en dólares</span></div>
      <div class="field"><label>Max trades simultáneos</label><input type="number" id="s-risk-max_simultaneous_trades"></div>
      <div class="field"><label>Daily loss limit (%)</label><input type="number" id="s-risk-daily_loss_limit_pct" step="0.01"><span class="hint">0.10 = 10%</span></div>
      <div class="field"><label>SL — ATR multiplier</label><input type="number" id="s-risk-sl_atr_multiplier" step="0.1"><span class="hint">Stop = ATR × este valor</span></div>
      <div class="field"><label>TP — ATR mín</label><input type="number" id="s-risk-tp_atr_multiplier_min" step="0.1"></div>
      <div class="field"><label>TP — ATR máx</label><input type="number" id="s-risk-tp_atr_multiplier_max" step="0.1"></div>
      <div class="field"><label>BE — Profit mínimo (USD)</label><input type="number" id="s-risk-breakeven_min_profit_usd"><span class="hint">Activa trailing al llegar aquí</span></div>
      <div class="field"><label>BE — Trail lock (USD)</label><input type="number" id="s-risk-trail_lock_usd"><span class="hint">SL trail $X detrás del máximo</span></div>
      <div class="field"><label>Cierre diario (hora UTC)</label><input type="number" id="s-risk-daily_close_utc"><span class="hint">20 = 4 PM ET</span></div>
    </div>
  </div>

  <!-- Noticias -->
  <div class="settings-group">
    <div class="group-header" onclick="toggleGroup(this)">
      <span class="group-title">📰 News Guard</span>
      <span class="group-chevron">▼</span>
    </div>
    <div class="group-body">
      <div class="field">
        <label>Habilitado</label>
        <select id="s-news-enabled"><option value="true">Sí</option><option value="false">No</option></select>
      </div>
      <div class="field"><label>Minutos antes de la noticia</label><input type="number" id="s-news-pre_news_minutes"><span class="hint">Aviso rojo X min antes</span></div>
      <div class="field"><label>Minutos después de la noticia</label><input type="number" id="s-news-post_news_minutes"><span class="hint">Aviso rojo X min después</span></div>
      <div class="field">
        <label>Filtro de impacto</label>
        <select id="s-news-impact_filter">
          <option value="High">Solo Alto Impacto</option>
          <option value="Medium">Medio + Alto</option>
        </select>
      </div>
    </div>
  </div>

  <div class="save-bar">
    <button class="btn-primary" onclick="saveSettings()">💾 Guardar configuración</button>
    <button class="btn-secondary" onclick="loadSettings()">↺ Recargar</button>
    <span id="save-msg" class="save-msg"></span>
  </div>
  <div class="restart-note">⚠️ Reinicia el bot para que los cambios tomen efecto.</div>

</div><!-- /page-settings -->

<!-- ══════════════════════════════════════════════════════════
     REPORTES
═════════════════════════════════════════════════════════════ -->
<div id="page-reports" class="page">

  <div class="tab-bar">
    <button class="tab-btn active" onclick="showReportTab('training')">🧠 Último Training</button>
    <button class="tab-btn" onclick="showReportTab('ops')">📊 Operaciones</button>
  </div>

  <!-- Training tab -->
  <div id="tab-training" class="tab-content active">
    <div id="training-content"><div class="no-data">Cargando…</div></div>
  </div>

  <!-- Operations tab -->
  <div id="tab-ops" class="tab-content">
    <div class="period-btns">
      <button class="period-btn active" onclick="setOpsPeriod('day',this)">
        <span class="p-icon">☀️</span><span class="p-label">Hoy</span>
      </button>
      <button class="period-btn" onclick="setOpsPeriod('week',this)">
        <span class="p-icon">📅</span><span class="p-label">Semana</span>
      </button>
      <button class="period-btn" onclick="setOpsPeriod('month',this)">
        <span class="p-icon">🗓</span><span class="p-label">Mes</span>
      </button>
    </div>

    <!-- Day -->
    <div id="ops-day">
      <div id="day-summary" class="report-cards" style="margin-bottom:16px"></div>
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
          <div class="label">Operaciones del día</div>
          <div style="display:flex;align-items:center;gap:8px">
            <span class="ts">Filas por página:</span>
            <select id="day-pagesize" onchange="setPageSize(+this.value)"
              style="background:var(--bg3);border:1px solid var(--border2);color:var(--text);font-family:var(--mono);font-size:11px;padding:3px 7px;border-radius:4px;outline:none">
              <option value="10">10</option>
              <option value="20">20</option>
              <option value="50">50</option>
            </select>
          </div>
        </div>
        <div id="day-table"></div>
        <div class="pagination-bar" id="day-pagination"></div>
      </div>
    </div>

    <!-- Week -->
    <div id="ops-week" style="display:none">
      <div class="period-nav-bar">
        <button class="nav-arrow" onclick="changeWeekNav(-1)">&#8249;</button>
        <span id="week-nav-label" class="period-nav-label"></span>
        <button class="nav-arrow" onclick="changeWeekNav(1)">&#8250;</button>
      </div>
      <div id="week-content"><div class="no-data">Cargando…</div></div>
    </div>

    <!-- Month -->
    <div id="ops-month" style="display:none">
      <div class="period-nav-bar">
        <button class="nav-arrow" onclick="changeMonthNav(-1)">&#8249;</button>
        <span id="month-nav-label" class="period-nav-label"></span>
        <button class="nav-arrow" onclick="changeMonthNav(1)">&#8250;</button>
      </div>
      <div id="month-content"><div class="no-data">Cargando…</div></div>
    </div>
  </div>

</div><!-- /page-reports -->

<!-- ══════════════════════════════════════════════════════════
     PERFIL
═════════════════════════════════════════════════════════════ -->
<div id="page-profile" class="page">
  <div class="card">
    <div class="label" style="margin-bottom:18px">👤 Perfil de usuario</div>
    <div class="field"><label>Nombre</label><input type="text" id="p-name" style="width:100%"></div>
    <div class="field"><label>Email</label><input type="email" id="p-email" style="width:100%"></div>
    <div class="field">
      <label>Zona horaria</label>
      <select id="p-timezone" style="width:100%">
        <option>America/New_York</option>
        <option>America/Chicago</option>
        <option>America/Los_Angeles</option>
        <option>America/Denver</option>
        <option>Europe/London</option>
        <option>Europe/Madrid</option>
        <option>UTC</option>
      </select>
    </div>
    <div class="field"><label>Notas</label><textarea id="p-notes"></textarea></div>
    <div class="save-bar" style="margin-top:4px">
      <button class="btn-primary" onclick="saveProfile()">💾 Guardar</button>
      <span id="profile-msg" class="save-msg"></span>
    </div>
  </div>
</div><!-- /page-profile -->

<script>
// ── Utilities ────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const fmt = (n, d=2) => n == null ? '--' : Number(n).toFixed(d);
const cc  = n => n > 0 ? 'green' : n < 0 ? 'red' : '';

// ── Navigation ───────────────────────────────────────────────────────────────
let _activePage = 'dashboard';
function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  $('page-' + name).classList.add('active');
  document.querySelectorAll('.nav-btn')[
    ['dashboard','settings','reports','profile'].indexOf(name)
  ].classList.add('active');
  _activePage = name;
  if (name === 'settings')  loadSettings();
  if (name === 'reports')   { loadTrainingReport(); setOpsPeriod('day', document.querySelector('.period-btn')); }
  if (name === 'profile')   loadProfile();
}

function toggleGroup(hdr) {
  const body = hdr.nextElementSibling;
  const chev = hdr.querySelector('.group-chevron');
  body.classList.toggle('collapsed');
  chev.style.transform = body.classList.contains('collapsed') ? 'rotate(-90deg)' : '';
}

// ── Dashboard polling ─────────────────────────────────────────────────────────
function renderDashboard(s) {
  const badge = $('mode-badge');
  if (s.paper) { badge.textContent='PAPER'; badge.className='badge paper'; }
  else          { badge.textContent='LIVE';  badge.className='badge live'; }

  const dev = (s.device||'cpu').toLowerCase();
  const db  = $('device-badge');
  db.textContent = dev === 'cuda' ? 'GPU' : 'CPU';
  db.className   = 'badge ' + (dev === 'cuda' ? 'gpu' : 'cpu');

  $('last-update').textContent = new Date().toLocaleTimeString();
  const dot = $('update-dot');
  dot.classList.add('flash'); setTimeout(() => dot.classList.remove('flash'), 300);

  const eq = s.equity || 0, dd = s.drawdown || 0;
  $('equity').textContent = (eq>=0?'+':'')+'$'+fmt(eq);
  $('equity').className   = 'value ' + cc(eq);
  $('dd-sub').textContent = 'DD: $'+fmt(dd);
  $('dd-sub').style.color = dd < 0 ? 'var(--red)' : 'var(--muted)';

  if (s.win_rate != null) {
    $('winrate').textContent = s.win_rate+'%';
    $('winrate').className   = 'value '+(s.win_rate>=50?'green':'red');
  }
  $('trades-sub').textContent = (s.total_trades||0)+' trades';

  $('atr').textContent  = fmt(s.atr);
  const chop = s.chop || 0;
  $('chop-sub').textContent = 'chop '+fmt(chop,3);
  $('chop-sub').style.color = chop>0.7?'var(--red)':chop>0.5?'var(--amber)':'var(--green)';

  $('price').textContent  = fmt(s.price,1);
  $('bar-sub').textContent = 'bar '+(s.bar_time||'--').slice(11,16);

  const sig   = s.signal||'SKIP';
  const sigEl = $('signal-val');
  sigEl.textContent = sig;
  sigEl.className   = 'sig-value '+(sig==='LONG'?'green':sig==='SHORT'?'red':'amber');

  const prob = (s.prob||0)*100;
  $('prob-pct').textContent = fmt(prob,1)+'%';
  const bar = $('prob-bar');
  bar.style.width      = prob+'%';
  bar.style.background = prob>=62
    ? (sig==='LONG'?'var(--green)':sig==='SHORT'?'var(--red)':'var(--amber)')
    : 'var(--muted)';

  const wrap = $('open-wrap');
  if (s.open_trade) {
    const t   = s.open_trade;
    const cls = t.direction==='LONG'?'long':'short';
    const clr = t.direction==='LONG'?'var(--green)':'var(--red)';
    const pnl = t.pnl||0;
    wrap.innerHTML = `
      <div class="open-banner ${cls}">
        <div><div class="sig-label">Trade abierto</div>
          <div class="sig-value" style="color:${clr};font-size:20px">${t.direction}</div></div>
        <div class="divider"></div>
        <div><div class="ts">Entry</div><div style="font-size:15px;font-weight:500">${fmt(t.entry,1)}</div></div>
        <div><div class="ts">SL</div><div style="font-size:15px;color:var(--red)">${fmt(t.sl,1)}</div></div>
        <div><div class="ts">TP</div><div style="font-size:15px;color:var(--green)">${fmt(t.tp,1)}</div></div>
        <div style="margin-left:auto"><div class="ts">Float P&L</div>
          <div style="font-size:18px;font-weight:600" class="${cc(pnl)}">${pnl>=0?'+':''}$${fmt(pnl)}</div></div>
      </div>`;
  } else { wrap.innerHTML = ''; }

  const trades = (s.trades||[]).slice().reverse();
  const listEl = $('trade-list');
  if (!trades.length) {
    listEl.innerHTML = '<div class="no-data">Sin trades aún</div>';
  } else {
    listEl.innerHTML = `<table class="trade-table">
      <thead><tr><th>Hora</th><th>Dir</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Razón</th></tr></thead>
      <tbody>${trades.slice(0,15).map(t=>`
        <tr>
          <td class="ts">${(t.time||'').slice(11,16)}</td>
          <td style="color:${t.direction==='LONG'?'var(--green)':'var(--red)'}">${t.direction}</td>
          <td>${fmt(t.entry,1)}</td><td>${fmt(t.exit,1)}</td>
          <td class="${t.pnl>=0?'pnl-pos':'pnl-neg'}">${t.pnl>=0?'+':''}$${fmt(t.pnl)}</td>
          <td class="ts">${t.reason||''}</td>
        </tr>`).join('')}
      </tbody></table>`;
  }

  const sigs  = s.signals||{LONG:0,SHORT:0,SKIP:0};
  const total = (sigs.LONG||0)+(sigs.SHORT||0)+(sigs.SKIP||0)||1;
  $('dist').innerHTML = [
    {name:'LONG',  color:'var(--green)', count:sigs.LONG ||0},
    {name:'SHORT', color:'var(--red)',   count:sigs.SHORT||0},
    {name:'SKIP',  color:'var(--muted)', count:sigs.SKIP ||0},
  ].map(({name,color,count})=>`
    <div class="dist-row">
      <span class="dist-name" style="color:${color}">${name}</span>
      <div class="dist-bar-bg"><div class="dist-bar-fill" style="width:${(count/total*100).toFixed(1)}%;background:${color}"></div></div>
      <span class="dist-count">${count}</span>
    </div>`).join('');
  $('bars-count').textContent = s.bars_seen||0;
}

async function pollDashboard() {
  try {
    const r = await fetch('/state');
    if (r.ok) renderDashboard(await r.json());
  } catch(e) {}
  setTimeout(pollDashboard, 2000);
}

// ── News ──────────────────────────────────────────────────────────────────────
function renderNews(data) {
  const events = (data.events||[]);
  const banner = $('news-alert-banner');
  const list   = $('news-list');

  // Banner for imminent event
  const hot = events.find(e => e.minutes_until >= -15 && e.minutes_until <= 10);
  const warn = !hot && events.find(e => e.minutes_until > 10 && e.minutes_until <= 30);
  if (hot) {
    const min = Math.round(hot.minutes_until);
    banner.innerHTML = `<div class="news-alert">⛔ NO OPERAR — ${hot.title} ${min<=0?'hace '+Math.abs(min)+' min':'en '+min+' min'}</div>`;
  } else if (warn) {
    banner.innerHTML = `<div class="news-warn">⚠️ Precaución — ${warn.title} en ${Math.round(warn.minutes_until)} min</div>`;
  } else {
    banner.innerHTML = '';
  }

  const upcoming = events.filter(e => e.minutes_until > -60).slice(0,6);
  if (!upcoming.length) {
    list.innerHTML = '<div class="no-data">Sin eventos de alto impacto próximos esta semana</div>';
    return;
  }

  list.innerHTML = upcoming.map(e => {
    const min = Math.round(e.minutes_until);
    let dotCls = 'future';
    if (min >= -15 && min <= 10) dotCls = 'hot';
    else if (min > 10 && min <= 30) dotCls = 'near';
    else if (min < -15) dotCls = 'past';

    const timeLabel = min <= 0
      ? `<span style="color:var(--red)">hace ${Math.abs(min)}m</span>`
      : `<span style="color:var(--muted2)">${min < 60 ? min+'m' : Math.round(min/60)+'h'}</span>`;

    const meta = [
      e.forecast ? 'Fcst: '+e.forecast : '',
      e.previous ? 'Prev: '+e.previous : '',
      e.actual   ? '<span style="color:var(--green)">Act: '+e.actual+'</span>' : '',
    ].filter(Boolean).join(' &nbsp;·&nbsp; ');

    return `<div class="news-row">
      <div class="news-dot ${dotCls}"></div>
      <span class="news-time">${e.time||''}</span>
      <span class="news-title">${e.title}</span>
      <span class="news-meta">${meta} &nbsp; ${timeLabel}</span>
    </div>`;
  }).join('');
}

async function pollNews() {
  try {
    const r = await fetch('/news');
    if (r.ok) renderNews(await r.json());
  } catch(e) {}
  setTimeout(pollNews, 300000); // refresh every 5 min
}

// ── Settings ──────────────────────────────────────────────────────────────────
async function loadSettings() {
  try {
    const cfg = await (await fetch('/config')).json();
    const mt5  = cfg.mt5  || {};
    const data = cfg.data || {};
    const mod  = cfg.model || {};
    const risk = cfg.risk || {};
    const news = cfg.news || {};

    // MT5
    const set = (id, v) => { const el=$(id); if(el && v!=null) el.value=v; };
    set('s-mt5-login',          mt5.login);
    set('s-mt5-password',       mt5.password||'****');
    set('s-mt5-server',         mt5.server);
    set('s-mt5-symbol',         mt5.symbol);
    set('s-mt5-timeframe',      mt5.timeframe);
    set('s-mt5-htf_timeframe',  mt5.htf_timeframe);
    set('s-mt5-magic',          mt5.magic);
    set('s-mt5-contract_size',  mt5.contract_size);
    set('s-mt5-terminal_path',  mt5.terminal_path||'');
    // Data
    set('s-data-lookback_bars',         data.lookback_bars);
    set('s-data-feature_window',        data.feature_window);
    set('s-data-retrain_interval_hours',data.retrain_interval_hours);
    set('s-data-min_bars_to_trade',     data.min_bars_to_trade);
    // Model
    set('s-model-device',                   mod.device);
    set('s-model-confidence_threshold',     mod.confidence_threshold);
    set('s-model-sr_proximity_pct',         mod.sr_proximity_pct);
    set('s-model-counter_trend_boost',      mod.counter_trend_boost);
    set('s-model-flip_confidence_threshold',mod.flip_confidence_threshold);
    set('s-model-chop_atr_ratio',           mod.chop_atr_ratio);
    set('s-model-label_lookahead',          mod.label_lookahead);
    set('s-model-label_momentum_bars',      mod.label_momentum_bars);
    set('s-model-label_threshold_atr',      mod.label_threshold_atr);
    // Risk
    set('s-risk-risk_per_trade_usd',      risk.risk_per_trade_usd);
    set('s-risk-max_simultaneous_trades', risk.max_simultaneous_trades);
    set('s-risk-daily_loss_limit_pct',    risk.daily_loss_limit_pct);
    set('s-risk-sl_atr_multiplier',       risk.sl_atr_multiplier);
    set('s-risk-tp_atr_multiplier_min',   risk.tp_atr_multiplier_min);
    set('s-risk-tp_atr_multiplier_max',   risk.tp_atr_multiplier_max);
    set('s-risk-breakeven_min_profit_usd',risk.breakeven_min_profit_usd);
    set('s-risk-trail_lock_usd',          risk.trail_lock_usd);
    set('s-risk-daily_close_utc',         risk.daily_close_utc);
    // News
    set('s-news-enabled',          String(news.enabled !== false));
    set('s-news-pre_news_minutes', news.pre_news_minutes  ?? 10);
    set('s-news-post_news_minutes',news.post_news_minutes ?? 15);
    set('s-news-impact_filter',    news.impact_filter     || 'High');
  } catch(e) {
    $('save-msg').textContent = 'Error al cargar: '+e.message;
    $('save-msg').className = 'save-msg err';
  }
}

async function saveSettings() {
  const gv = id => {
    const el = $(id); if (!el) return undefined;
    return el.type === 'number' ? (el.value===''?undefined:Number(el.value))
         : el.type === 'checkbox' ? el.checked
         : el.value;
  };
  const cfg = {
    mt5: {
      login:          gv('s-mt5-login'),
      password:       gv('s-mt5-password'),
      server:         gv('s-mt5-server'),
      symbol:         gv('s-mt5-symbol'),
      timeframe:      gv('s-mt5-timeframe'),
      htf_timeframe:  gv('s-mt5-htf_timeframe'),
      magic:          gv('s-mt5-magic'),
      contract_size:  gv('s-mt5-contract_size'),
      terminal_path:  gv('s-mt5-terminal_path'),
    },
    data: {
      lookback_bars:          gv('s-data-lookback_bars'),
      feature_window:         gv('s-data-feature_window'),
      retrain_interval_hours: gv('s-data-retrain_interval_hours'),
      min_bars_to_trade:      gv('s-data-min_bars_to_trade'),
    },
    model: {
      device:                    gv('s-model-device'),
      confidence_threshold:      gv('s-model-confidence_threshold'),
      sr_proximity_pct:          gv('s-model-sr_proximity_pct'),
      counter_trend_boost:       gv('s-model-counter_trend_boost'),
      flip_confidence_threshold: gv('s-model-flip_confidence_threshold'),
      chop_atr_ratio:            gv('s-model-chop_atr_ratio'),
      label_lookahead:           gv('s-model-label_lookahead'),
      label_momentum_bars:       gv('s-model-label_momentum_bars'),
      label_threshold_atr:       gv('s-model-label_threshold_atr'),
    },
    risk: {
      risk_per_trade_usd:       gv('s-risk-risk_per_trade_usd'),
      max_simultaneous_trades:  gv('s-risk-max_simultaneous_trades'),
      daily_loss_limit_pct:     gv('s-risk-daily_loss_limit_pct'),
      sl_atr_multiplier:        gv('s-risk-sl_atr_multiplier'),
      tp_atr_multiplier_min:    gv('s-risk-tp_atr_multiplier_min'),
      tp_atr_multiplier_max:    gv('s-risk-tp_atr_multiplier_max'),
      breakeven_min_profit_usd: gv('s-risk-breakeven_min_profit_usd'),
      trail_lock_usd:           gv('s-risk-trail_lock_usd'),
      daily_close_utc:          gv('s-risk-daily_close_utc'),
      allowed_sessions:         [[5,19]],
    },
    news: {
      enabled:           gv('s-news-enabled') === 'true',
      pre_news_minutes:  gv('s-news-pre_news_minutes'),
      post_news_minutes: gv('s-news-post_news_minutes'),
      impact_filter:     gv('s-news-impact_filter'),
    },
    logging: { log_file: 'logs/trades.log', model_metrics_file: 'logs/model_metrics.log', level: 'INFO' },
  };
  try {
    const r = await fetch('/config', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(cfg)});
    const res = await r.json();
    const msg = $('save-msg');
    if (res.ok) { msg.textContent='✓ Guardado'; msg.className='save-msg ok'; }
    else        { msg.textContent='Error: '+(res.error||'?'); msg.className='save-msg err'; }
    setTimeout(() => { msg.textContent=''; }, 4000);
  } catch(e) {
    $('save-msg').textContent='Error: '+e.message;
    $('save-msg').className='save-msg err';
  }
}

// ── Reports ───────────────────────────────────────────────────────────────────
function showReportTab(tab) {
  document.querySelectorAll('.tab-btn').forEach((b,i)=>
    b.classList.toggle('active', ['training','ops'][i]===tab));
  document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active'));
  $('tab-'+tab).classList.add('active');
  if (tab==='training') loadTrainingReport();
}

async function loadTrainingReport() {
  const el = $('training-content');
  el.innerHTML = '<div class="no-data">Cargando…</div>';
  try {
    const d = await (await fetch('/reports/training')).json();
    if (d.error) { el.innerHTML=`<div class="no-data">${d.error}</div>`; return; }

    const labelsHtml = d.labels ? `
      <div class="label-pills">
        <span class="lpill l">LONG: ${d.labels.LONG?.toLocaleString()}</span>
        <span class="lpill s">SHORT: ${d.labels.SHORT?.toLocaleString()}</span>
        <span class="lpill k">SKIP: ${d.labels.SKIP?.toLocaleString()}</span>
      </div>` : '';

    const foldsHtml = (d.folds||[]).length ? `
      <table class="fold-table">
        <thead><tr><th>Fold</th><th>LONG f1</th><th>SHORT f1</th><th>Accuracy</th></tr></thead>
        <tbody>${(d.folds||[]).map(f=>`
          <tr>
            <td>${f.fold}</td>
            <td class="${f.long_f1>=0.5?'green':'red'}">${fmt(f.long_f1,3)}</td>
            <td class="${f.short_f1>=0.5?'green':'red'}">${fmt(f.short_f1,3)}</td>
            <td class="${f.acc>=0.55?'green':'amber'}">${fmt(f.acc,3)}</td>
          </tr>`).join('')}
        </tbody>
      </table>` : '';

    el.innerHTML = `
      <div class="card" style="margin-bottom:16px">
        <div class="label" style="margin-bottom:12px">
          Último entrenamiento: <span style="color:var(--text)">${d.train_date||'--'}</span>
        </div>
        ${d.avg_accuracy != null ? `<div style="margin-bottom:14px">
          <span class="ts">Walk-forward avg accuracy:</span>
          <span style="font-size:20px;font-weight:600;margin-left:10px" class="${d.avg_accuracy>=0.55?'green':'amber'}">${fmt(d.avg_accuracy*100,1)}%</span>
        </div>` : ''}
        ${labelsHtml}
        ${foldsHtml}
      </div>`;
  } catch(e) {
    el.innerHTML = `<div class="no-data">Error: ${e.message}</div>`;
  }
}

// ── Operations — state ───────────────────────────────────────────────────────
const MONTHS = ['Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic'];
let _opsPeriod = 'day';
let _opsYear   = new Date().getFullYear();
let _opsMonth  = new Date().getMonth() + 1;
let _dayTrades = [], _dayPage = 1, _dayPageSize = 10;

function setOpsPeriod(period, btn) {
  document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  _opsPeriod = period;
  $('ops-day').style.display   = period === 'day'   ? '' : 'none';
  $('ops-week').style.display  = period === 'week'  ? '' : 'none';
  $('ops-month').style.display = period === 'month' ? '' : 'none';
  if (period === 'day')   loadDayReport();
  if (period === 'week')  loadWeekReport();
  if (period === 'month') loadMonthReport();
}

// ── Day ───────────────────────────────────────────────────────────────────────
async function loadDayReport() {
  try {
    const d = await (await fetch('/reports/trades?period=day')).json();
    if (d.error) { $('day-table').innerHTML=`<div class="no-data">${d.error}</div>`; return; }
    _dayTrades = d.trades || [];
    _dayPage   = 1;
    const pnlClr = d.net_pnl >= 0 ? 'var(--green)' : 'var(--red)';
    $('day-summary').innerHTML = `
      <div class="rep-card"><div class="rc-label">Win Rate</div>
        <div class="rc-value green">${d.win_pct}%</div>
        <div class="rc-sub">${_dayTrades.filter(t=>t.pnl>0).length} ganados</div></div>
      <div class="rep-card"><div class="rc-label">Loss Rate</div>
        <div class="rc-value red">${d.loss_pct}%</div>
        <div class="rc-sub">${_dayTrades.filter(t=>t.pnl<0).length} perdidos</div></div>
      <div class="rep-card"><div class="rc-label">P&L Neto</div>
        <div class="rc-value" style="color:${pnlClr}">${d.net_pnl>=0?'+':''}$${fmt(d.net_pnl)}</div>
        <div class="rc-sub">${d.total} trades</div></div>`;
    renderDayTable();
  } catch(e) { $('day-table').innerHTML=`<div class="no-data">Error: ${e.message}</div>`; }
}

function setPageSize(n) { _dayPageSize = n; _dayPage = 1; renderDayTable(); }

function renderDayTable() {
  const start  = (_dayPage - 1) * _dayPageSize;
  const slice  = _dayTrades.slice(start, start + _dayPageSize);
  const total  = _dayTrades.length;
  const nPages = Math.ceil(total / _dayPageSize);

  $('day-table').innerHTML = slice.length ? `
    <table class="trade-table">
      <thead><tr><th>Hora</th><th>Dir</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Razón</th></tr></thead>
      <tbody>${slice.map(t=>`<tr>
        <td class="ts">${(t.open_time||'').slice(11,16)}</td>
        <td style="color:${t.direction==='LONG'?'var(--green)':'var(--red)'}">${t.direction}</td>
        <td>${fmt(t.entry,1)}</td><td>${fmt(t.exit,1)}</td>
        <td class="${t.pnl>=0?'pnl-pos':'pnl-neg'}">${t.pnl>=0?'+':''}$${fmt(t.pnl)}</td>
        <td class="ts">${t.reason||''}</td>
      </tr>`).join('')}</tbody>
    </table>` : '<div class="no-data">Sin trades hoy</div>';

  if (total === 0) { $('day-pagination').innerHTML=''; return; }
  const prev = `<button class="page-btn" onclick="goDayPage(${_dayPage-1})" ${_dayPage===1?'disabled':''}>&#8249;</button>`;
  const next = `<button class="page-btn" onclick="goDayPage(${_dayPage+1})" ${_dayPage===nPages?'disabled':''}>&#8250;</button>`;
  let nums = '';
  for (let i=1; i<=nPages; i++)
    nums += `<button class="page-btn ${i===_dayPage?'cur':''}" onclick="goDayPage(${i})">${i}</button>`;
  $('day-pagination').innerHTML = `
    <div class="page-btns">${prev}${nums}${next}</div>
    <span class="ts">${start+1}&ndash;${Math.min(start+_dayPageSize,total)} de ${total}</span>`;
}

function goDayPage(p) {
  const n = Math.ceil(_dayTrades.length / _dayPageSize);
  if (p < 1 || p > n) return;
  _dayPage = p; renderDayTable();
}

// ── Week ──────────────────────────────────────────────────────────────────────
async function loadWeekReport() {
  $('week-nav-label').textContent = `${MONTHS[_opsMonth-1]} ${_opsYear}`;
  $('week-content').innerHTML = '<div class="no-data">Cargando…</div>';
  try {
    const d = await (await fetch(`/reports/weeks?year=${_opsYear}&month=${_opsMonth}`)).json();
    if (d.error || !d.weeks?.length) {
      $('week-content').innerHTML = '<div class="no-data">Sin operaciones este mes</div>'; return;
    }
    $('week-content').innerHTML = `<div class="summary-grid">${d.weeks.map(w => {
      const clr = w.net_pnl >= 0 ? 'var(--green)' : 'var(--red)';
      return `<div class="summary-label">
        <div class="sl-title">Semana ${w.week} <span style="color:var(--muted);font-weight:400;font-size:10px">${w.range}</span></div>
        <div class="sl-rates"><span class="sl-win">WIN ${w.win_pct}%</span><span style="color:var(--muted2);margin:0 4px">|</span><span class="sl-loss">LOSS ${w.loss_pct}%</span></div>
        <div class="sl-pnl" style="color:${clr}">${w.net_pnl>=0?'+':''}$${fmt(w.net_pnl)}</div>
        <div class="sl-sub">${w.total} operaciones</div>
      </div>`;
    }).join('')}</div>`;
  } catch(e) { $('week-content').innerHTML=`<div class="no-data">Error: ${e.message}</div>`; }
}

function changeWeekNav(delta) {
  _opsMonth += delta;
  if (_opsMonth > 12) { _opsMonth = 1;  _opsYear++; }
  if (_opsMonth < 1)  { _opsMonth = 12; _opsYear--; }
  loadWeekReport();
}

// ── Month ─────────────────────────────────────────────────────────────────────
async function loadMonthReport() {
  $('month-nav-label').textContent = String(_opsYear);
  $('month-content').innerHTML = '<div class="no-data">Cargando…</div>';
  try {
    const d = await (await fetch(`/reports/months?year=${_opsYear}`)).json();
    $('month-content').innerHTML = `<div class="month-grid">${(d.months||[]).map((m,i) => {
      if (!m.total) return `<div class="month-label empty"><div class="ml-name">${MONTHS[i]}</div><div class="ml-sub">—</div></div>`;
      const clr = m.net_pnl >= 0 ? 'var(--green)' : 'var(--red)';
      return `<div class="month-label">
        <div class="ml-name">${MONTHS[i]}</div>
        <div class="ml-rates"><span class="ml-win">W ${m.win_pct}%</span></div>
        <div class="ml-rates"><span class="ml-loss">L ${m.loss_pct}%</span></div>
        <div class="ml-pnl" style="color:${clr}">${m.net_pnl>=0?'+':''}$${fmt(m.net_pnl)}</div>
        <div class="ml-sub">${m.total} trades</div>
      </div>`;
    }).join('')}</div>`;
  } catch(e) { $('month-content').innerHTML=`<div class="no-data">Error: ${e.message}</div>`; }
}

function changeMonthNav(delta) { _opsYear += delta; loadMonthReport(); }

// ── Profile ───────────────────────────────────────────────────────────────────
async function loadProfile() {
  try {
    const p = await (await fetch('/profile')).json();
    $('p-name').value     = p.name     || '';
    $('p-email').value    = p.email    || '';
    $('p-timezone').value = p.timezone || 'America/New_York';
    $('p-notes').value    = p.notes    || '';
  } catch(e) {}
}

async function saveProfile() {
  const data = {
    name:     $('p-name').value,
    email:    $('p-email').value,
    timezone: $('p-timezone').value,
    notes:    $('p-notes').value,
  };
  try {
    const r = await fetch('/profile', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    const res = await r.json();
    const msg = $('profile-msg');
    if (res.ok) { msg.textContent='✓ Guardado'; msg.className='save-msg ok'; }
    else        { msg.textContent='Error'; msg.className='save-msg err'; }
    setTimeout(()=>{ msg.textContent=''; }, 3000);
  } catch(e) {}
}

// ── Boot ──────────────────────────────────────────────────────────────────────
pollDashboard();
pollNews();
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
