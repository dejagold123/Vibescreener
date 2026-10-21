"""Read-only dashboard and JSON API, served from the same process as the indexer.

Each request opens its own read-only SQLite connection, so the dashboard never
blocks or corrupts the indexer's writes. Heavy queries are cached for a few seconds.
Everything here is GET-only. Names and symbols come from the chain and are
untrusted; the page escapes them before showing them.
"""
import json
import os
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config as cfg
from . import report

STATIC = os.path.join(os.path.dirname(__file__), "static")
ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
CACHE_SECONDS = 10


class ReadDB:
    """Per-request read-only handle with the same q() helper the report code uses."""

    def __init__(self, path):
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only = ON")

    def q(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params)]

    def close(self):
        self.conn.close()


_cache = {}
_cache_lock = threading.Lock()


def cached(key, fn, ttl=CACHE_SECONDS):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def _label(rows, db=None):
    """Add a readable `quote` name. Some tokens are quoted in another launched token;
    those are shown by that token's symbol instead of a raw address."""
    known = {}
    if db is not None:
        odd = sorted({r["quote_token"] for r in rows if r.get("quote_token")
                      and r["quote_token"] not in (cfg.ZERO, cfg.VIBEVIBE_TOKEN)})
        for i in range(0, len(odd), 400):
            part = odd[i:i + 400]
            for k in db.q(f"SELECT token, symbol FROM tokens WHERE token IN ({','.join('?' * len(part))})", part):
                known[k["token"]] = k["symbol"]
    for r in rows:
        if "quote_token" in r:
            q = r["quote_token"]
            r["quote"] = known.get(q) or report.qlabel(q)
            r["quote_is_token"] = q in known
    return rows


def _num(value, default, lo, hi):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


# ---- data layer: plain functions over a db with .q() -------------------------

def overview(db, hours=24):
    since = int(time.time()) - hours * 3600
    one = lambda sql, p=(): db.q(sql, p)[0]
    out = {
        "tokens": one("SELECT COUNT(*) n FROM tokens")["n"],
        "trades": one("SELECT COUNT(*) n FROM trades")["n"],
        "trades_window": one("SELECT COUNT(*) n FROM trades WHERE ts >= ?", (since,))["n"],
        "wallets_window": one("SELECT COUNT(DISTINCT trader) n FROM trades WHERE ts >= ?", (since,))["n"],
        "launches_window": one("SELECT COUNT(*) n FROM tokens WHERE ts >= ?", (since,))["n"],
        "hours": hours,
        "last_block": one("SELECT MAX(block) b FROM trades")["b"],
        "last_trade_ts": one("SELECT MAX(ts) t FROM trades")["t"],
        "first_trade_ts": one("SELECT MIN(ts) t FROM trades")["t"],
        "now": int(time.time()),
    }
    vol = db.q("""SELECT k.quote_token AS quote_token, SUM(t.quote_amt) AS volume, COUNT(*) AS trades
                  FROM trades t LEFT JOIN tokens k ON k.token = t.token
                  WHERE t.ts >= ? GROUP BY k.quote_token""", (since,))
    main = (cfg.ZERO, cfg.VIBEVIBE_TOKEN)
    out["volume_window"] = _label([v for v in vol if v["quote_token"] in main])
    # volume in other quote tokens has no common unit, so it is counted but never added up
    out["other_quote_trades"] = sum(v["trades"] for v in vol if v["quote_token"] not in main)
    ex = one("SELECT * FROM pnl_excluded")
    out["pnl_excluded"] = {"incomplete_history": ex["incomplete_history"] or 0,
                           "unknown_quote": ex["unknown_quote"] or 0,
                           "total_positions": ex["total_positions"] or 0}
    return out


def trending(db, hours=24, limit=20):
    return _label(report.trending_tokens(db, hours, limit), db)


QUOTES = {"eth": cfg.ZERO, "vibevibe": cfg.VIBEVIBE_TOKEN}


def wallets(db, limit=20, quote="eth"):
    """Ranked within one quote token only; amounts in different tokens are not comparable."""
    q = QUOTES.get(quote, cfg.ZERO)
    return _label(db.q("SELECT * FROM wallet_pnl WHERE trades > 0 AND quote_token = ? "
                       "ORDER BY pnl_quote DESC LIMIT ?", (q, limit)))


def creators(db, limit=20):
    return report.top_creators(db, limit)


def feed(db, limit=30):
    """Newest trades and launches interleaved, newest first."""
    trades = db.q("""SELECT 'trade' AS kind, t.side, t.trader, t.token, k.symbol, k.quote_token,
                            t.quote_amt AS amount, t.token_amt, t.ts, t.block, t.tx
                     FROM trades t LEFT JOIN tokens k ON k.token = t.token
                     ORDER BY t.block DESC, t.log_index DESC LIMIT ?""", (limit,))
    launches = db.q("""SELECT 'launch' AS kind, NULL AS side, creator AS trader, token, symbol,
                              quote_token, NULL AS amount, NULL AS token_amt, ts, block, tx
                       FROM tokens WHERE ts IS NOT NULL ORDER BY ts DESC LIMIT ?""", (limit,))
    items = sorted(trades + launches, key=lambda r: r["ts"] or 0, reverse=True)[:limit]
    return _label(items, db)


def token_detail(db, addr):
    addr = addr.lower()
    rows = db.q("SELECT * FROM token_stats WHERE token = ?", (addr,))
    if not rows:
        return None
    info = _label(rows, db)[0]
    info["curve"] = (db.q("SELECT curve, uri, tx FROM tokens WHERE token = ?", (addr,)) or [{}])[0]
    info["price_series"] = db.q(
        """SELECT ts, quote_amt / token_amt AS price FROM trades
           WHERE token = ? AND token_amt > 0 ORDER BY block DESC, log_index DESC LIMIT 300""", (addr,))[::-1]
    info["recent_trades"] = db.q(
        """SELECT side, trader, quote_amt AS amount, token_amt, ts, tx FROM trades
           WHERE token = ? ORDER BY block DESC, log_index DESC LIMIT 25""", (addr,))
    info["holders_seen"] = db.q(
        """SELECT trader, SUM(CASE WHEN side='buy' THEN token_amt ELSE -token_amt END) AS net_tokens,
                  SUM(CASE WHEN side='buy' THEN quote_amt ELSE 0 END) AS spent,
                  SUM(CASE WHEN side='sell' THEN quote_amt ELSE 0 END) AS received
           FROM trades WHERE token = ? GROUP BY trader ORDER BY spent DESC LIMIT 15""", (addr,))
    return info


def wallet_detail(db, addr):
    addr = addr.lower()
    pos = db.q("SELECT * FROM wallet_token_pnl WHERE trader = ? ORDER BY trades DESC LIMIT 100", (addr,))
    if not pos:
        return None
    for p in pos:
        p["pnl"] = (p["quote_received"] + max(p["tokens_bought"] - p["tokens_sold"], 0)
                    * (p["last_price"] or 0) - p["quote_spent"]) if p["complete"] and p["quote_token"] else None
    return {"wallet": addr, "positions": _label(pos, db),
            "summary": _label(db.q("SELECT * FROM wallet_pnl WHERE trader = ?", (addr,))),
            "created": db.q("SELECT token, symbol, name, ts FROM tokens WHERE creator = ? ORDER BY ts DESC LIMIT 20",
                            (addr,))}


# ---- HTTP --------------------------------------------------------------------

def route(db_path, path, query):
    """Return (status, payload) for an API path."""
    hours = _num(query.get("hours", [None])[0], 24, 1, 24 * 30)
    limit = _num(query.get("limit", [None])[0], 20, 1, 100)
    db = ReadDB(db_path)
    try:
        if path == "/api/overview":
            return 200, cached(("ov", hours), lambda: overview(db, hours))
        if path == "/api/trending":
            return 200, cached(("tr", hours, limit), lambda: trending(db, hours, limit))
        if path == "/api/wallets":
            quote = (query.get("quote", ["eth"])[0] or "eth").lower()
            return 200, cached(("w", limit, quote), lambda: wallets(db, limit, quote))
        if path == "/api/creators":
            return 200, cached(("c", limit), lambda: creators(db, limit))
        if path == "/api/feed":
            return 200, feed(db, _num(query.get("limit", [None])[0], 30, 1, 100))
        m = re.match(r"^/api/(token|wallet)/(.+)$", path)
        if m:
            if not ADDR.match(m.group(2)):
                return 400, {"error": "address must be 0x followed by 40 hex characters"}
            fn = token_detail if m.group(1) == "token" else wallet_detail
            data = fn(db, m.group(2))
            return (200, data) if data else (404, {"error": "not found"})
        return 404, {"error": "unknown endpoint"}
    except sqlite3.OperationalError as e:
        # database empty / not created yet, or briefly locked
        return 503, {"error": f"data not ready ({e})"}
    finally:
        db.close()


def make_handler(db_path):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body, ctype):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/health":
                return self._send(200, b"ok", "text/plain")
            if u.path.startswith("/api/"):
                status, payload = route(db_path, u.path, parse_qs(u.query))
                return self._send(status, json.dumps(payload).encode(), "application/json")
            self._send(404, b"not found", "text/plain")

        def log_message(self, *a):
            pass

    return Handler


def serve(db_path, port, host="0.0.0.0"):
    return ThreadingHTTPServer((host, port), make_handler(db_path))


def start_background(db_path, port, log=print):
    """Start the dashboard in a thread. A port problem must never stop indexing."""
    try:
        server = serve(db_path, port)
    except OSError as e:
        log(f"dashboard not started ({e})")
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"dashboard listening on port {port}")
    return server
