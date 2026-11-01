"""Watchlists and Telegram alerts (a paid 30-day plan).

A signed-in wallet buys the plan with credits, links a Telegram chat by sending the bot a one-time
code, and watches up to 10 tokens or wallets. The worker then messages the chat when a watched token
moves by the chosen percentage in an hour, or a watched wallet trades. Messages are plain text built
only from indexed figures and sanitised symbols; no user-controlled text is ever sent unescaped, and
no parse mode is used, so nothing can turn into markup or a link.
"""
import re
import secrets
import threading
import time

import requests

from . import ask, brief, credits
from . import config as cfg

MAX_WATCHES = 10
CODE_TTL = 900
TOKEN_COOLDOWN = 3600
ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_bot = {"name": ""}


class AlertError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---- Telegram ----------------------------------------------------------------

class Telegram:
    def __init__(self, token, session=None):
        self.base, self.s = f"https://api.telegram.org/bot{token}", session or requests

    def _call(self, method, payload, timeout=35):
        try:
            r = self.s.post(f"{self.base}/{method}", json=payload, timeout=timeout)
            body = r.json()
        except (requests.RequestException, ValueError) as e:
            raise AlertError(f"telegram {method}: {type(e).__name__}", 502)
        if not body.get("ok"):
            raise AlertError(f"telegram {method}: {str(body.get('description'))[:80]}", 502)
        return body["result"]

    def me(self):
        return self._call("getMe", {}, timeout=15)

    def updates(self, offset, wait=25):
        return self._call("getUpdates", {"offset": offset, "timeout": wait, "allowed_updates": ["message"]}, wait + 10)

    def send(self, chat_id, text):
        return self._call("sendMessage", {"chat_id": chat_id, "text": text[:3500], "disable_web_page_preview": True}, 15)


# ---- watches & linking -------------------------------------------------------

def _need_plan(db, wallet, now=None):
    if not credits.plan_expires(db, wallet, "alerts", now):
        raise AlertError("Alerts need an active alerts plan. Buy one with credits first.", 402)


def buy(db, wallet, settings=None, now=None):
    st = settings or cfg.credits_settings()
    exp = credits.buy_plan(db, wallet, "alerts", st["alerts_micro"], st["plan_days"], now)
    if not exp:
        raise AlertError(f"The alerts plan costs {st['alerts_price']} {st['symbol']} for {st['plan_days']} days. "
                         "Add credits first.", 402)
    return exp


def new_link_code(db, wallet, now=None):
    now = int(now or time.time())
    code = secrets.token_hex(4).upper()
    with db.conn:
        db.conn.execute("DELETE FROM tg_codes WHERE wallet = ? OR expires < ?", (wallet, now))
        db.conn.execute("INSERT INTO tg_codes VALUES (?,?,?)", (code, wallet, now + CODE_TTL))
    return code, now + CODE_TTL


def link_chat(db, code, chat_id, now=None):
    """Consume a code and link the chat to its wallet. Returns the wallet, or None for a bad/old code."""
    code = (code or "").strip().upper()
    if not re.fullmatch(r"[0-9A-F]{8}", code):
        return None
    now = int(now or time.time())
    with db.conn:
        row = db.conn.execute("SELECT wallet FROM tg_codes WHERE code = ? AND expires >= ?", (code, now)).fetchone()
        if not row:
            return None
        db.conn.execute("DELETE FROM tg_codes WHERE code = ?", (code,))
        db.conn.execute("INSERT INTO tg_links VALUES (?,?) ON CONFLICT(wallet) DO UPDATE SET chat_id = ?",
                        (row[0], str(chat_id), str(chat_id)))
    return row[0]


def add_watch(db, read_db, wallet, kind, target, threshold=None, now=None):
    _need_plan(db, wallet, now)
    if kind not in ("token", "wallet"):
        raise AlertError("Watch a token or a wallet.")
    if not ADDR.match(target or ""):
        raise AlertError("That doesn't look like an address.")
    target = target.lower()
    if kind == "token" and not read_db.q("SELECT 1 FROM tokens WHERE token = ?", (target,)):
        raise AlertError("We haven't seen that token.", 404)
    try:
        th = float(threshold) if threshold not in (None, "") else 20.0
    except (TypeError, ValueError):
        raise AlertError("The percentage must be a number.")
    if not 5 <= th <= 1000:
        raise AlertError("Pick a percentage between 5 and 1000.")
    n = db.q("SELECT COUNT(*) n FROM watches WHERE wallet = ?", (wallet,))[0]["n"]
    exists = db.q("SELECT 1 FROM watches WHERE wallet = ? AND kind = ? AND target = ?", (wallet, kind, target))
    if n >= MAX_WATCHES and not exists:
        raise AlertError(f"You can watch up to {MAX_WATCHES} things at once.")
    with db.conn:
        db.conn.execute("""INSERT INTO watches(wallet, kind, target, threshold, last_seen) VALUES (?,?,?,?,?)
                           ON CONFLICT(wallet, kind, target) DO UPDATE SET threshold = ?""",
                        (wallet, kind, target, th, int(now or time.time()), th))


def remove_watch(db, wallet, watch_id):
    with db.conn:
        db.conn.execute("DELETE FROM watches WHERE id = ? AND wallet = ?", (int(watch_id), wallet))


def overview(db, read_db, wallet, now=None):
    rows = db.q("SELECT id, kind, target, threshold FROM watches WHERE wallet = ? ORDER BY id", (wallet,))
    for r in rows:
        r["label"] = brief.web_short(r["target"])
        if r["kind"] == "token":
            t = read_db.q("SELECT symbol FROM tokens WHERE token = ?", (r["target"],))
            if t:
                r["label"] = brief.clean_symbol(t[0]["symbol"], r["target"])
    linked = bool(db.q("SELECT 1 FROM tg_links WHERE wallet = ?", (wallet,)))
    return {"plan_expires": credits.plan_expires(db, wallet, "alerts", now), "linked": linked, "watches": rows,
            "max": MAX_WATCHES}


def handle(wdb, rdb, wallet, action, data, settings=None):
    """The POST endpoints, after the caller has proven which wallet it is."""
    st = settings or cfg.credits_settings()
    if not st["enabled"]:
        raise AlertError("Credits are not switched on for this site yet.", 404)
    if action == "buy":
        return {"plan_expires": buy(wdb, wallet, st), "balance": credits.tokens_text(credits.balance(wdb, wallet))}
    if action == "link":
        if not st["telegram"]:
            raise AlertError("Telegram alerts are not set up on this site yet.", 404)
        code, exp = new_link_code(wdb, wallet)
        return {"code": code, "expires": exp, "bot": _bot["name"] or None}
    if action == "watch":
        add_watch(wdb, rdb, wallet, str(data.get("kind", "")), str(data.get("target", "")), data.get("threshold"))
        return overview(wdb, rdb, wallet)
    if action == "unwatch":
        try:
            remove_watch(wdb, wallet, data.get("id"))
        except (TypeError, ValueError):
            raise AlertError("Unknown watch.")
        return overview(wdb, rdb, wallet)
    if action == "me":
        return overview(wdb, rdb, wallet)
    raise AlertError("Unknown action.", 404)


# ---- checking and sending ----------------------------------------------------

def _sym(sym, token=None):
    """Chat apps turn 'name.com' into a clickable link, so a token named like a website could carry a
    phishing link in an alert. Dots are removed from symbols in every message."""
    return brief.clean_symbol(sym, token).replace(".", "")


def _price_line(t):
    return f"{ask.price_text(t['last_price'])} {t['quote']}"


def check(write_db, read_db, send, now=None):
    """One pass over every active watch. `send(chat_id, text)` delivers. Returns how many messages went out."""
    from . import web
    now = int(now or time.time())
    sent = 0
    rows = write_db.q("""SELECT w.id, w.wallet, w.kind, w.target, w.threshold, w.last_alert, w.last_seen, l.chat_id
                         FROM watches w JOIN tg_links l ON l.wallet = w.wallet
                         JOIN plans p ON p.wallet = w.wallet AND p.kind = 'alerts' AND p.expires >= ?""", (now,))
    for w in rows:
        try:
            if w["kind"] == "token":
                if now - w["last_alert"] < TOKEN_COOLDOWN:
                    continue
                t = web.token_detail(read_db, w["target"])
                ch = t and t["stats"]["ch"].get("1h")
                if ch is None or abs(ch) < w["threshold"]:
                    continue
                sym = _sym(t["symbol"], w["target"])
                send(w["chat_id"], f"{sym} moved {brief.pct(ch)} in the last hour. Last price {_price_line(t)}. "
                                   "VibeScreener testnet alert, not financial advice.")
                write_db.conn.execute("UPDATE watches SET last_alert = ? WHERE id = ?", (now, w["id"]))
                sent += 1
            else:
                trades = web._label(read_db.q("""SELECT t.side, t.ts, t.token, tk.symbol, t.quote_amt AS amount,
                                                        tk.quote_token
                                                 FROM trades t LEFT JOIN tokens tk ON tk.token = t.token
                                                 WHERE t.trader = ? AND t.ts > ? ORDER BY t.ts DESC LIMIT 5""",
                                              (w["target"], w["last_seen"])), read_db)
                if not trades:
                    continue
                lines = [f"{'Bought' if x['side'] == 'buy' else 'Sold'} {_sym(x['symbol'], x['token'])} "
                         f"for {brief.n(x['amount'])} {_sym(x['quote'], '')} ({ask.age_text(x['ts'], now)})"
                         for x in trades]
                send(w["chat_id"], f"Watched wallet {brief.web_short(w['target'])} traded:\n" + "\n".join(lines) +
                     "\nVibeScreener testnet alert, not financial advice.")
                write_db.conn.execute("UPDATE watches SET last_seen = ?, last_alert = ? WHERE id = ?",
                                      (max(x["ts"] for x in trades), now, w["id"]))
                sent += 1
        except AlertError:
            continue                               # one broken chat must not stop the others
        finally:
            write_db.conn.commit()
    return sent


def handle_updates(db, tg, updates, now=None):
    """Answer messages sent to the bot: a code links the chat. Returns the next update offset."""
    nxt = 0
    for u in updates:
        nxt = max(nxt, u["update_id"] + 1)
        m = u.get("message") or {}
        chat = (m.get("chat") or {}).get("id")
        text = (m.get("text") or "").strip()
        if chat is None or (m.get("chat") or {}).get("type") != "private":
            continue
        code = text.split()[-1] if text.lower().startswith("/start") and len(text.split()) > 1 else text
        wallet = link_chat(db, code, chat, now)
        try:
            tg.send(chat, f"Linked to wallet {brief.web_short(wallet)}. You'll get alerts for what you watch on VibeScreener."
                    if wallet else "Send the 8-character code shown on VibeScreener's Alerts page to link this chat.")
        except AlertError:
            pass
    return nxt


def worker(db_path, log=print, stop=None, tg=None, every=60):
    from .db import DB
    from .web import ReadDB, WriteDB
    stop = stop or threading.Event()
    tg = tg or Telegram(cfg._clean(__import__("os").environ.get("TELEGRAM_BOT_TOKEN")))
    try:
        _bot["name"] = cfg._clean(__import__("os").environ.get("TELEGRAM_BOT_USERNAME")) or tg.me().get("username", "")
    except AlertError as e:
        log(f"alerts: telegram not reachable yet ({e})")
    wdb, rdb = WriteDB(db_path), ReadDB(db_path)
    offset = int(DB(db_path).get_state("tg_offset", "0"))
    last_check = 0.0
    while not stop.is_set():
        try:
            ups = tg.updates(offset, wait=20)
            if ups:
                offset = handle_updates(wdb, tg, ups)
                wdb.conn.execute("INSERT INTO state VALUES ('tg_offset', ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                                 (str(offset), str(offset)))
                wdb.conn.commit()
            if time.time() - last_check >= every:
                last_check = time.time()
                n = check(wdb, rdb, tg.send)
                if n:
                    log(f"alerts: sent {n} message(s)")
        except AlertError as e:
            log(f"alerts paused ({str(e)[:80]})")
            stop.wait(15)
        except Exception as e:                      # never let this thread touch indexing
            log(f"alerts error ({type(e).__name__})")
            stop.wait(15)


def start_background(db_path, log=print):
    st = cfg.credits_settings()
    if not (st["enabled"] and st["telegram"]):
        return None
    log("alerts: Telegram alerts are on")
    t = threading.Thread(target=worker, args=(db_path, log), daemon=True)
    t.start()
    return t
