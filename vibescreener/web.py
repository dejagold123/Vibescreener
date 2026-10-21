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
from . import meta, report

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


def _img(rows):
    """Turn a stored image content id into gateway URLs the browser can load (it tries them in
    order). Only ids that pass the content-id check ever become URLs."""
    for r in rows:
        cid = r.pop("image_cid", None)
        r["image_urls"] = [g + cid for g in cfg.IPFS_GATEWAYS] if cid and meta.CID.match(cid) else []
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
        SELECT w.token AS token, k.symbol AS symbol, k.quote_token AS quote_token, k.ts AS launched_ts, m.image_cid AS image_cid,
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
        FROM w LEFT JOIN tokens k ON k.token = w.token LEFT JOIN token_meta m ON m.token = w.token
        WHERE (? = 0 OR k.ts >= ?)
        GROUP BY w.token HAVING COUNT(*) >= ? AND (? != 'change' OR change_pct IS NOT NULL)
        ORDER BY {order} LIMIT ?""", (since, 1 if new_only else 0, since, floor, sort, limit))
    return _img(_label(rows, db))


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


# ---- token table (DexScreener-style) ------------------------------------------

WINDOWS = {"5m": 300, "1h": 3600, "6h": 21600, "24h": 86400}


def _changes(series, now):
    """Price change per window, in percent. `series` is [(ts, price)] oldest first.
    Baseline = the last price before the window opened (so a move that started earlier still
    counts), else the first price inside it. None when there is nothing to compare."""
    out = {}
    for label, secs in WINDOWS.items():
        cut = now - secs
        inside = [p for t, p in series if t >= cut]
        before = [p for t, p in series if t < cut]
        base = before[-1] if before else (inside[0] if len(inside) >= 2 else None)
        out[label] = (100.0 * (inside[-1] / base - 1)) if inside and base else None
    return out


def _windows(trades, now):
    """trades: [(ts, side, trader, quote_amt, price)] oldest first, last 24h. Returns per-window
    counts plus price changes. Price series uses buys only when there are 2+ (a buy and a sell
    differ by the fee, which would read as a fake move)."""
    out = {}
    for label, secs in WINDOWS.items():
        part = [t for t in trades if t[0] >= now - secs]
        buys = [t for t in part if t[1] == "buy"]
        out[label] = {"txns": len(part), "buys": len(buys), "sells": len(part) - len(buys),
                      "volume": sum(t[3] for t in part), "buy_volume": sum(t[3] for t in buys),
                      "makers": len({t[2] for t in part})}
    buys_only = [(t[0], t[4]) for t in trades if t[1] == "buy"]
    series = buys_only if len(buys_only) >= 2 else [(t[0], t[4]) for t in trades]
    return {"w": out, "ch": _changes(series, now),
            "price": trades[-1][4] if trades else None, "last_ts": trades[-1][0] if trades else None}


def _recent_by_token(db, now, only=None):
    sql = ("SELECT token, ts, side, trader, quote_amt, quote_amt / token_amt AS price FROM trades "
           "WHERE ts >= ? AND token_amt > 0")
    params = [now - 86400]
    if only:
        sql += " AND token = ?"
        params.append(only)
    by = {}
    for r in db.conn.execute(sql + " ORDER BY token, block, log_index", params):
        by.setdefault(r[0], []).append((r[1], r[2], r[3], r[4], r[5]))
    return by


PAIR_SORTS = ("trending", "new", "gainers", "volume")


def pairs(db, quote="eth", sort="trending", window="24h", limit=100):
    """One row per token for the main table. Ranking (volume) is only meaningful inside one
    quote token, so quote=all falls back to ranking volume by trades."""
    now = int(time.time())
    window = window if window in WINDOWS else "24h"
    sort = sort if sort in PAIR_SORTS else "trending"
    want = QUOTES.get(quote)                                    # None = all quote tokens
    stats = {tok: _windows(tr, now) for tok, tr in _recent_by_token(db, now).items()}
    toks = set(stats)
    if sort == "new":                                           # new launches show even before their first trade
        toks |= {r["token"] for r in db.q("SELECT token FROM tokens WHERE ts >= ?", (now - 86400,))}
    toks = sorted(toks)
    info = {}
    for i in range(0, len(toks), 400):
        part = toks[i:i + 400]
        for r in db.q(f"""SELECT k.token, k.symbol, k.name, k.quote_token, k.ts AS launched_ts, m.image_cid
                          FROM tokens k LEFT JOIN token_meta m ON m.token = k.token
                          WHERE k.token IN ({','.join('?' * len(part))})""", part):
            info[r["token"]] = r
    rows = []
    for tok in toks:
        k = info.get(tok, {"token": tok, "symbol": None, "name": None, "quote_token": None,
                           "launched_ts": None, "image_cid": None})
        if want is not None and k["quote_token"] != want:
            continue
        st = stats.get(tok) or _windows([], now)
        w = st["w"][window]
        if sort != "new" and w["txns"] == 0:
            continue
        price = st["price"]
        if price is None:
            lp = db.q("SELECT price FROM token_last_price WHERE token = ?", (tok,))
            price = lp[0]["price"] if lp else None
        rows.append({**k, "price": price, "last_ts": st["last_ts"], "txns": w["txns"], "buys": w["buys"],
                     "sells": w["sells"], "volume": w["volume"], "buy_volume": w["buy_volume"],
                     "makers": w["makers"], "change": st["ch"][window],
                     **{"ch" + lbl: v for lbl, v in st["ch"].items()}})
    if sort == "gainers":
        rows = [r for r in rows if r["change"] is not None and r["txns"] >= 5 and r["makers"] >= 3]
        rows.sort(key=lambda r: r["change"], reverse=True)
    elif sort == "volume" and want is not None:
        rows.sort(key=lambda r: r["volume"], reverse=True)
    elif sort == "new":
        rows.sort(key=lambda r: r["launched_ts"] or 0, reverse=True)
    else:
        rows.sort(key=lambda r: (r["makers"], r["txns"]), reverse=True)
    rows = rows[:limit]
    return {"window": window, "sort": sort, "quote": quote if want is not None else "all",
            "volume_comparable": want is not None, "rows": _img(_label(rows, db))}


INTERVALS = (60, 300, 900, 3600)


def candles(db, addr, interval=300, count=200):
    """OHLC candles from trade prices (fees ignored). Empty buckets are filled flat so the
    time axis stays honest."""
    interval = interval if interval in INTERVALS else 300
    rows = db.q("""SELECT ts, side, quote_amt AS q, quote_amt / token_amt AS price FROM trades
                   WHERE token = ? AND token_amt > 0 AND ts IS NOT NULL
                   ORDER BY block DESC, log_index DESC LIMIT 5000""", (addr.lower(),))[::-1]
    by = {}
    for r in rows:
        b = r["ts"] // interval * interval
        c = by.get(b)
        if c is None:
            by[b] = {"t": b, "o": r["price"], "h": r["price"], "l": r["price"], "c": r["price"],
                     "v": r["q"], "buys": int(r["side"] == "buy"), "sells": int(r["side"] != "buy")}
        else:
            c["h"], c["l"], c["c"] = max(c["h"], r["price"]), min(c["l"], r["price"]), r["price"]
            c["v"] += r["q"]
            c["buys" if r["side"] == "buy" else "sells"] += 1
    if not by:
        return {"interval": interval, "candles": []}
    out, prev = [], None
    for b in range(min(by), max(by) + interval, interval):
        out.append(by.get(b) or {"t": b, "o": prev, "h": prev, "l": prev, "c": prev, "v": 0, "buys": 0, "sells": 0})
        prev = out[-1]["c"]
    return {"interval": interval, "candles": out[-count:]}


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
    m = db.q("SELECT description, image_cid, socials FROM token_meta WHERE token = ? AND status = 'ok'", (addr,))
    info.update(_img(m)[0] if m else {"description": "", "image_urls": [], "socials": "{}"})
    try:
        socials = json.loads(info.get("socials") or "{}")
    except ValueError:
        socials = {}
    info["socials"] = {k: v for k, v in socials.items() if str(v).lower().startswith(("http://", "https://"))}
    now = int(time.time())
    info["stats"] = _windows(_recent_by_token(db, now, addr).get(addr, []), now)
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
        if path == "/api/pairs":
            q = lambda k, d: (query.get(k, [d])[0] or d).lower()
            return 200, cached(("pairs", q("quote", "eth"), q("sort", "trending"), q("window", "24h"), limit),
                               lambda: pairs(db, q("quote", "eth"), q("sort", "trending"), q("window", "24h"), limit), ttl=6)
        m = re.match(r"^/api/token/([^/]+)/candles$", path)
        if m:
            if not ADDR.match(m.group(1)):
                return 400, {"error": "address must be 0x followed by 40 hex characters"}
            return 200, candles(db, m.group(1), _num(query.get("interval", [None])[0], 300, 60, 3600))
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
