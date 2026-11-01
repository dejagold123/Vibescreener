"""Paid data access: a 30-day API key that unlocks bulk CSV exports.

The dashboard's normal JSON endpoints stay public and free. A key adds full-table CSV downloads
(tokens, trades, wallets, creators) that are too heavy to leave open. Keys are shown once and only
their hash is stored; a key works while its wallet has an active API plan. Every cell is written
so a spreadsheet can't run it as a formula, and each key has its own request limit."""
import csv
import hashlib
import io
import re
import secrets
import time

from . import credits
from . import web

MAX_ROWS = 5000
DEFAULT_ROWS = 1000
KEY_RE = re.compile(r"^vsk_[0-9a-f]{40}$")
ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")


class ExportError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def new_key(db, wallet, now=None):
    """Create (replacing any old one) a key for a wallet with an active plan. Returns the key, once."""
    if not credits.plan_expires(db, wallet, "api", now):
        raise ExportError("API access needs an active API plan. Buy one with credits first.", 402)
    key = "vsk_" + secrets.token_hex(20)
    with db.conn:
        db.conn.execute("DELETE FROM api_keys WHERE wallet = ?", (wallet,))
        db.conn.execute("INSERT INTO api_keys VALUES (?,?,?,?)", (_hash(key), wallet, key[:8], 0))
    return key


def wallet_for_key(db, key, now=None):
    if not key or not KEY_RE.match(key):
        return None
    r = db.q("SELECT wallet FROM api_keys WHERE hash = ?", (_hash(key),))
    if not r or not credits.plan_expires(db, r[0]["wallet"], "api", now):
        return None
    return r[0]["wallet"]


def info(db, wallet, now=None):
    k = db.q("SELECT prefix FROM api_keys WHERE wallet = ?", (wallet,))
    return {"plan_expires": credits.plan_expires(db, wallet, "api", now), "key_prefix": k[0]["prefix"] if k else None}


def cell(v):
    """Make one value safe for a spreadsheet: text starting with = + - @ or a control character is
    prefixed with an apostrophe so Excel/Sheets show it instead of evaluating it."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v) if isinstance(v, float) else str(v)
    t = str(v).replace("\r", " ").replace("\n", " ")
    return "'" + t if t[:1] in ("=", "+", "-", "@", "\t") or (t and ord(t[0]) < 32) else t


def to_csv(columns, rows):
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([cell(r.get(c)) for c in columns])
    return out.getvalue()


def _limit(query):
    try:
        n = int((query.get("limit", [""])[0]) or DEFAULT_ROWS)
    except ValueError:
        n = DEFAULT_ROWS
    return max(1, min(MAX_ROWS, n))


def dataset(db, name, query):
    """(columns, rows) for one export, read-only."""
    lim = _limit(query)
    if name == "tokens":
        rows = db.q("""SELECT token, symbol, name, creator, quote_token, launched_ts, trades, traders,
                              volume_quote, last_price FROM token_stats ORDER BY launched_ts DESC LIMIT ?""", (lim,))
        return ["token", "symbol", "name", "creator", "quote_token", "launched_ts", "trades", "traders",
                "volume_quote", "last_price"], rows
    if name == "trades":
        token = (query.get("token", [""])[0] or "").lower()
        where, args = ("WHERE token = ?", [token]) if ADDR.match(token) else ("", [])
        if token and not where:
            raise ExportError("token must be an address.")
        rows = db.q(f"""SELECT ts, block, tx, log_index, side, trader, token, token_amt, quote_amt
                        FROM trades {where} ORDER BY ts DESC, log_index DESC LIMIT ?""", args + [lim])
        return ["ts", "block", "tx", "log_index", "side", "trader", "token", "token_amt", "quote_amt"], rows
    if name == "wallets":
        rows = db.q("""SELECT trader, COUNT(*) positions, SUM(trades) trades, SUM(quote_spent) quote_spent,
                              SUM(quote_received) quote_received
                       FROM wallet_token_pnl GROUP BY trader ORDER BY trades DESC LIMIT ?""", (lim,))
        return ["trader", "positions", "trades", "quote_spent", "quote_received"], rows
    if name == "creators":
        rows = db.q("""SELECT creator, tokens_launched, trades, unique_traders FROM creator_stats
                       ORDER BY tokens_launched DESC LIMIT ?""", (lim,))
        return ["creator", "tokens_launched", "trades", "unique_traders"], rows
    raise ExportError("Unknown dataset. Use tokens, trades, wallets or creators.", 404)


def respond(db_path, name, query, key):
    """GET /api/export/<name>.csv -> (status, body bytes, content type)."""
    wdb, rdb = web.WriteDB(db_path), web.ReadDB(db_path)
    try:
        wallet = wallet_for_key(wdb, key)
        if not wallet:
            raise ExportError("Missing, wrong or expired API key. Send it as 'Authorization: Bearer vsk_...'.", 401)
        if web.throttled("key:" + _hash(key)[:16], limit=20):
            raise ExportError("Slow down: 20 exports a minute per key.", 429)
        cols, rows = dataset(rdb, name, query)
        return 200, to_csv(cols, rows).encode(), "text/csv; charset=utf-8"
    except ExportError as e:
        return e.status, ('{"error": %s}' % __import__("json").dumps(str(e))).encode(), "application/json"
    finally:
        wdb.close()
        rdb.close()


def handle(wdb, wallet, action, settings):
    """POST /api/apikey/<action> for a signed-in wallet."""
    if not settings["enabled"]:
        raise ExportError("Credits are not switched on for this site yet.", 404)
    if action == "buy":
        exp = credits.buy_plan(wdb, wallet, "api", settings["api_micro"], settings["plan_days"])
        if not exp:
            raise ExportError(f"API access costs {settings['api_price']} {settings['symbol']} for "
                              f"{settings['plan_days']} days. Add credits first.", 402)
        return {"plan_expires": exp, "balance": credits.tokens_text(credits.balance(wdb, wallet))}
    if action == "new":
        return {"key": new_key(wdb, wallet), **info(wdb, wallet)}
    if action == "me":
        return info(wdb, wallet)
    raise ExportError("Unknown action.", 404)
