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


SORTS = {"traders": "traders DESC, trades DESC",
         "change": "change_pct DESC",
         "buys": "buy_share DESC, trades DESC"}


def trending(db, hours=24, limit=20, sort="traders", new_only=False):
    """Tokens active in the window, with price change and buy/sell pressure.
    Price change compares the first and last trade price in the window (fees ignored).
    The gainers and buy-pressure views ignore tokens with fewer than 5 trades,
    because one or two trades say nothing."""
    since = int(time.time()) - hours * 3600
    order = SORTS.get(sort, SORTS["traders"])
    sort = sort if sort in SORTS else "traders"
    floor = 5 if sort in ("change", "buys") else 1
    rows = db.q(f"""
        WITH w AS (
          SELECT token, trader, side, quote_amt, quote_amt / token_amt AS price,
                 ROW_NUMBER() OVER (PARTITION BY token ORDER BY block, log_index) AS rn_first,
                 ROW_NUMBER() OVER (PARTITION BY token ORDER BY block DESC, log_index DESC) AS rn_last
          FROM trades WHERE ts >= ? AND token_amt > 0)
        SELECT w.token AS token, k.symbol AS symbol, k.quote_token AS quote_token, k.ts AS launched_ts,
               COUNT(*) AS trades, COUNT(DISTINCT w.trader) AS traders,
               SUM(w.side = 'buy') AS buys, SUM(w.side = 'sell') AS sells,
               SUM(w.quote_amt) AS volume_quote,
               SUM(CASE WHEN w.side = 'buy' THEN w.quote_amt ELSE -w.quote_amt END) AS net_flow,
               1.0 * SUM(w.side = 'buy') / COUNT(*) AS buy_share,
               MAX(CASE WHEN w.rn_first = 1 THEN w.price END) AS first_price,
               MAX(CASE WHEN w.rn_last = 1 THEN w.price END) AS last_price,
               CASE WHEN COUNT(*) >= 2 AND MAX(CASE WHEN w.rn_first = 1 THEN w.price END) > 0
                    THEN 100.0 * (MAX(CASE WHEN w.rn_last = 1 THEN w.price END)
                                  / MAX(CASE WHEN w.rn_first = 1 THEN w.price END) - 1)
               END AS change_pct
        FROM w LEFT JOIN tokens k ON k.token = w.token
        WHERE (? = 0 OR k.ts >= ?)
        GROUP BY w.token HAVING COUNT(*) >= ? AND (? != 'change' OR change_pct IS NOT NULL)
        ORDER BY {order} LIMIT ?""", (since, 1 if new_only else 0, since, floor, sort, limit))
    return _label(rows, db)


QUOTES = {"eth": cfg.ZERO, "vibevibe": cfg.VIBEVIBE_TOKEN}


POSITION_PNL = ("quote_received + MAX(tokens_bought - tokens_sold, 0) * COALESCE(last_price, 0) - quote_spent")


def wallets(db, limit=20, quote="eth"):
    """Ranked within one quote token only; amounts in different tokens are not comparable.
    A position counts as a win when its estimated PnL (realized plus remaining tokens at
    the last price) is above zero. Only positions with a complete buy history are counted."""
    q = QUOTES.get(quote, cfg.ZERO)
    return _label(db.q(f"""
        SELECT p.*, s.positions, s.wins, 1.0 * s.wins / s.positions AS win_rate
        FROM wallet_pnl p
        JOIN (SELECT trader, quote_token, COUNT(*) AS positions, SUM(pnl > 0) AS wins
              FROM (SELECT trader, quote_token, {POSITION_PNL} AS pnl FROM wallet_token_pnl
                    WHERE complete = 1 AND quote_token = ?)
              GROUP BY trader, quote_token) s
          ON s.trader = p.trader AND s.quote_token = p.quote_token
        WHERE p.trades > 0 AND p.quote_token = ? ORDER BY p.pnl_quote DESC LIMIT ?""", (q, q, limit)))


CREATOR_SORTS = {"traders": "unique_traders DESC, trades DESC",
                 "hit": "hit_rate DESC, traded DESC",
                 "launched": "tokens_launched DESC, trades DESC"}


def creators(db, limit=20, sort="traders", hours=24):
    """Creators with how their tokens fared. A token counts as 'up' when its last trade
    price is above its first (needs 2+ trades) and as 'active' when it traded in the
    last `hours`. The hit-rate view only ranks creators with 3+ traded tokens."""
    since = int(time.time()) - hours * 3600
    order = CREATOR_SORTS.get(sort, CREATOR_SORTS["traders"])
    having = "HAVING traded >= 3" if sort == "hit" else "HAVING traded >= 1"
    return db.q(f"""
        WITH w AS (
          SELECT token, trader, ts, quote_amt / token_amt AS price,
                 ROW_NUMBER() OVER (PARTITION BY token ORDER BY block, log_index) AS rn_first,
                 ROW_NUMBER() OVER (PARTITION BY token ORDER BY block DESC, log_index DESC) AS rn_last
          FROM trades WHERE token_amt > 0),
        pt AS (
          SELECT token, COUNT(*) AS trades, MAX(ts) AS last_ts,
                 MAX(CASE WHEN rn_first = 1 THEN price END) AS first_price,
                 MAX(CASE WHEN rn_last = 1 THEN price END) AS last_price
          FROM w GROUP BY token)
        SELECT k.creator AS creator, COUNT(*) AS tokens_launched,
               COUNT(pt.token) AS traded,
               COALESCE(SUM(pt.trades >= 2 AND pt.last_price > pt.first_price), 0) AS up,
               COALESCE(SUM(pt.last_ts >= ?), 0) AS active,
               COALESCE(SUM(pt.trades), 0) AS trades,
               c.unique_traders AS unique_traders,
               CASE WHEN COUNT(pt.token) > 0
                    THEN 1.0 * COALESCE(SUM(pt.trades >= 2 AND pt.last_price > pt.first_price), 0) / COUNT(pt.token)
               END AS hit_rate
        FROM tokens k
        LEFT JOIN pt ON pt.token = k.token
        JOIN creator_stats c ON c.creator = k.creator
        GROUP BY k.creator {having}
        ORDER BY {order} LIMIT ?""", (since, limit))


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


def search(db, text, limit=10):
    """Find tokens by name/symbol/address and wallets by address."""
    text = (text or "").strip()[:80]
    out = {"tokens": [], "wallets": []}
    if len(text) < 2:
        return out
    if ADDR.match(text):
        a = text.lower()
        out["tokens"] = _label(db.q("SELECT token, symbol, name, quote_token, trades, traders FROM token_stats "
                                    "WHERE token = ?", (a,)), db)
        if db.q("SELECT 1 FROM trades WHERE trader = ? LIMIT 1", (a,)) or \
           db.q("SELECT 1 FROM tokens WHERE creator = ? LIMIT 1", (a,)):
            out["wallets"] = [{"wallet": a}]
        return out
    like = "%" + text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    out["tokens"] = _label(db.q(
        """SELECT token, symbol, name, quote_token, trades, traders FROM token_stats
           WHERE LOWER(COALESCE(symbol, '')) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(name, '')) LIKE ? ESCAPE '\\'
           ORDER BY (LOWER(symbol) = ?) DESC, traders DESC, trades DESC LIMIT ?""",
        (like, like, text.lower(), limit)), db)
    return out


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
    pos = db.q("SELECT * FROM wallet_token_pnl WHERE trader = ? ORDER BY trades DESC LIMIT 200", (addr,))
    created = db.q("""SELECT token, symbol, name, launched_ts AS ts, trades, traders FROM token_stats
                      WHERE creator = ? ORDER BY launched_ts DESC LIMIT 20""", (addr,))
    if not pos and not created:
        return None
    for p in pos:
        p["pnl"] = (p["quote_received"] + max(p["tokens_bought"] - p["tokens_sold"], 0)
                    * (p["last_price"] or 0) - p["quote_spent"]) if p["complete"] and p["quote_token"] else None
    _label(pos, db)
    stats = []
    for quote in sorted({p["quote_token"] for p in pos if p["pnl"] is not None}):
        mine = [p for p in pos if p["quote_token"] == quote and p["pnl"] is not None]
        best = max(mine, key=lambda p: p["pnl"])
        worst = min(mine, key=lambda p: p["pnl"])
        wins = sum(1 for p in mine if p["pnl"] > 0)
        brief = lambda p: {"token": p["token"], "symbol": p["symbol"], "pnl": p["pnl"]}
        stats.append({"quote_token": quote, "quote": mine[0]["quote"], "positions": len(mine), "wins": wins,
                      "win_rate": wins / len(mine), "pnl": sum(p["pnl"] for p in mine),
                      "spent": sum(p["quote_spent"] for p in mine), "best": brief(best), "worst": brief(worst)})
    span = db.q("SELECT MIN(ts) AS first_ts, MAX(ts) AS last_ts, COUNT(*) AS trades FROM trades WHERE trader = ?", (addr,))[0]
    return {"wallet": addr, "positions": pos, "stats": stats, "created": created, "activity": span,
            "excluded": sum(1 for p in pos if p["pnl"] is None),
            "positions_total": len(pos), "positions_truncated": len(pos) == 200}


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
            sort = query.get("sort", ["traders"])[0]
            new_only = query.get("new", ["0"])[0] == "1"
            return 200, cached(("tr", hours, limit, sort, new_only),
                               lambda: trending(db, hours, limit, sort, new_only))
        if path == "/api/wallets":
            quote = (query.get("quote", ["eth"])[0] or "eth").lower()
            return 200, cached(("w", limit, quote), lambda: wallets(db, limit, quote))
        if path == "/api/creators":
            sort = query.get("sort", ["traders"])[0]
            return 200, cached(("c", limit, sort, hours), lambda: creators(db, limit, sort, hours))
        if path == "/api/search":
            return 200, search(db, query.get("q", [""])[0])
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
