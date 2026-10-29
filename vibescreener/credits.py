"""Token credits for the Ask box.

A visitor signs in by signing a short message with their wallet (proves ownership, costs no gas), sends
some of the VibeScreener token to the treasury address, and the deposit watcher credits their balance.
Each paid question then spends credits. Balances are kept in millionths of a token, as whole numbers, so
there is no rounding drift. A deposit is credited once: (transaction, log position) is unique.

The watcher reads the explorer's token-transfer list, not the RPC node, so it adds no RPC load.
"""
import datetime
import hashlib
import re
import secrets
import threading
import time

from . import config as cfg
from . import ethsig

MICRO = 10 ** 6
NONCE_TTL = 600
SESSION_TTL = 7 * 86400
ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
CONFIRM_MARGIN = 50          # blocks re-read on every poll, so nothing slips through a page boundary
ZERO = "0x" + "0" * 40


class CreditsError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def tokens_text(micro):
    whole, frac = divmod(max(0, micro), MICRO)
    return f"{whole}" + (f".{frac:06d}".rstrip("0") if frac else "")


# ---- sign-in -------------------------------------------------------------------

def login_message(wallet, nonce, expires, domain=None, uri=None):
    """EIP-4361-style sign-in message (M6). The domain, URI, chain id and
    timestamps bind the signature to this service, so a signature obtained
    for another site (or for a bare wallet+nonce text) cannot be replayed
    here, and a signature for this site cannot be replayed elsewhere.

    Domain comes from VIBESCREENER_DOMAIN (see config.signin_domain); the
    default is a local placeholder, NOT a real deployed domain — deployers
    must set it to their Railway/public domain. Issued-at is derived as
    expires - NONCE_TTL, so login() can reconstruct this exact text from
    the stored nonce/expiry without a schema change; changing the domain
    or the TTL invalidates pending nonces, which is safe (they are
    one-time and expire in 600s anyway).
    """
    domain = domain or cfg.signin_domain()
    uri = uri or f"https://{domain}"
    fmt = lambda ts: datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (f"{domain} wants you to sign in with your Ethereum account:\n{wallet}\n\n"
            "VibeScreener sign-in. This message costs no gas and gives no access to your funds.\n\n"
            f"URI: {uri}\nVersion: 1\nChain ID: 46630\nNonce: {nonce}\n"
            f"Issued At: {fmt(expires - NONCE_TTL)}\nExpiration Time: {fmt(expires)}")


def issue_nonce(db, wallet, now=None):
    if not ADDR.match(wallet or ""):
        raise CreditsError("That doesn't look like a wallet address.")
    wallet, now = wallet.lower(), int(now or time.time())
    nonce, expires = secrets.token_hex(12), now + NONCE_TTL
    with db.conn:
        db.conn.execute("INSERT OR REPLACE INTO auth_nonces VALUES (?,?,?)", (wallet, nonce, expires))
    return login_message(wallet, nonce, expires)


def login(db, wallet, signature, now=None):
    """Verify the signature and return a session token (shown once; only its hash is stored)."""
    if not ADDR.match(wallet or ""):
        raise CreditsError("That doesn't look like a wallet address.")
    wallet, now = wallet.lower(), int(now or time.time())
    row = db.q("SELECT nonce, expires FROM auth_nonces WHERE wallet = ?", (wallet,))
    if not row or row[0]["expires"] < now:
        raise CreditsError("Sign-in expired. Start again.", 401)
    message = login_message(wallet, row[0]["nonce"], row[0]["expires"])
    with db.conn:
        db.conn.execute("DELETE FROM auth_nonces WHERE wallet = ?", (wallet,))     # one try per nonce
    if ethsig.recover_personal(message, str(signature or "")) != wallet:
        raise CreditsError("That signature doesn't match this wallet.", 401)
    token = secrets.token_hex(32)
    with db.conn:
        db.conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        db.conn.execute("INSERT INTO sessions VALUES (?,?,?)",
                        (hashlib.sha256(token.encode()).hexdigest(), wallet, now + SESSION_TTL))
    return token


def wallet_for(db, token, now=None):
    if not token or not re.fullmatch(r"[0-9a-f]{64}", token):
        return None
    r = db.q("SELECT wallet FROM sessions WHERE token_hash = ? AND expires >= ?",
             (hashlib.sha256(token.encode()).hexdigest(), int(now or time.time())))
    return r[0]["wallet"] if r else None


# ---- balances ------------------------------------------------------------------

def balance(db, wallet):
    r = db.q("SELECT micro FROM credits WHERE wallet = ?", (wallet,))
    return r[0]["micro"] if r else 0


def recent_deposits(db, wallet, limit=5):
    rows = db.q("SELECT tx, micro, ts FROM deposits WHERE wallet = ? ORDER BY ts DESC LIMIT ?", (wallet, limit))
    return [{"tx": r["tx"], "amount": tokens_text(r["micro"]), "ts": r["ts"]} for r in rows]


def take(db, wallet, micro):
    """Spend `micro` if the wallet has it. Atomic, so two simultaneous questions can't both spend one balance."""
    with db.conn:
        cur = db.conn.execute("UPDATE credits SET micro = micro - ? WHERE wallet = ? AND micro >= ?",
                              (micro, wallet, micro))
    return cur.rowcount == 1


def refund(db, wallet, micro):
    with db.conn:
        db.conn.execute("INSERT INTO credits VALUES (?,?) ON CONFLICT(wallet) DO UPDATE SET micro = micro + ?",
                        (wallet, micro, micro))


def credit_deposit(db, tx, log_index, wallet, micro, ts):
    """Credit a deposit exactly once. Returns True when it was new."""
    with db.conn:
        cur = db.conn.execute("INSERT OR IGNORE INTO deposits VALUES (?,?,?,?,?)",
                              (tx, log_index, wallet, micro, ts))
        if cur.rowcount != 1:
            return False
        db.conn.execute("INSERT INTO credits VALUES (?,?) ON CONFLICT(wallet) DO UPDATE SET micro = micro + ?",
                        (wallet, micro, micro))
    return True


# ---- deposit watcher -----------------------------------------------------------

def _ts(text):
    try:
        return int(datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return int(time.time())


def poll(db, explorer, settings, pages=20, log=print, confirmations=0, head_block=None):
    """One pass over the token's newest transfers; credits any new transfer to the treasury.

    M14: items missing transaction_hash/log_index (or with unparseable fields)
    are skipped and logged, never allowed to abort the whole poll and stall
    the watcher on the same page. When confirmations > 0 and a chain head is
    known (head_block, or the explorer's block number if it exposes one),
    only transfers at least that many blocks behind the head are credited,
    so a reorged transfer cannot leave credit behind. Default confirmations
    is 0 to preserve the existing unit-test behaviour; production should
    pass a positive value (see worker()).
    M8: credit_scan_block only advances to the newest block on a completed
    scan. If the page cap is hit, it advances only to the oldest block seen
    on the last scanned page, so the next poll re-scans the overlap
    (idempotent via the (tx, log_index) key) instead of skipping deposits
    beyond page 20 forever.
    """
    token, treasury = settings["token"], settings["treasury"]
    scan_from = int(db.get_state("credit_scan_block", "0"))
    if head_block is None and confirmations:
        try:
            head_block = int(explorer.get("/api/v2/stats", None).get("total_blocks") or 0) or None
        except Exception:
            head_block = None
        if not head_block:
            log("credits: WARNING: could not determine chain head; crediting without "
                "confirmation depth on this poll (reorg risk until the head is available)")
    params, head, new, page = None, 0, [], 0
    oldest_seen = None
    completed = False
    while page < pages:
        data = explorer.get(f"/api/v2/tokens/{token}/transfers", params)
        items = data.get("items") or []
        stop = False
        for it in items:
            try:
                blk = int(it.get("block_number") or 0)
            except (ValueError, TypeError):
                log(f"credits: skipping malformed transfer item (bad block_number): {str(it)[:120]}")
                continue
            head = max(head, blk)
            oldest_seen = blk if oldest_seen is None else min(oldest_seen, blk)
            if scan_from and blk < scan_from - CONFIRM_MARGIN:
                stop = True
                break
            if confirmations and head_block and blk > head_block - confirmations:
                continue  # not yet confirmed; a later poll will credit it
            to, frm = (it.get("to") or {}).get("hash", ""), (it.get("from") or {}).get("hash", "")
            if to.lower() != treasury or not ADDR.match(frm or "") or frm.lower() in (ZERO, treasury):
                continue
            total = it.get("total") or {}
            try:
                value, decimals = int(total["value"]), int(total.get("decimals") or 18)
                tx_hash, log_index = it["transaction_hash"], int(it["log_index"])
            except (KeyError, ValueError, TypeError):
                # M14: skip-and-log, do not abort the poll.
                log(f"credits: skipping malformed transfer item: {str(it)[:160]}")
                continue
            micro = value * MICRO // 10 ** decimals
            if micro > 0 and credit_deposit(db, tx_hash, log_index, frm.lower(),
                                            micro, _ts(it.get("timestamp"))):
                new.append((frm.lower(), micro))
        page += 1
        nxt = data.get("next_page_params")
        if stop or not nxt:
            completed = True
            break
        params = nxt
    if not completed:
        log("credits: ALERT: page limit reached before the scan completed; "
            "credit_scan_block will only advance to the oldest block seen, "
            "so the next poll re-scans the overlap instead of skipping deposits")
    if completed and head:
        db.set_state("credit_scan_block", head)
    elif not completed and oldest_seen is not None:
        db.set_state("credit_scan_block", oldest_seen)
    return new


def worker(db_path, log=print, stop=None, every=20, explorer=None):
    from .db import DB
    from .explorer import Explorer
    stop = stop or threading.Event()
    db, explorer = DB(db_path), explorer or Explorer()
    while not stop.is_set():
        st = cfg.credits_settings()
        try:
            for wallet, micro in poll(db, explorer, st, log=log, confirmations=12):
                log(f"credits: {tokens_text(micro)} credited to {wallet[:6]}…{wallet[-4:]}")
        except Exception as e:                       # never let the watcher touch indexing
            log(f"credits watcher paused ({str(e)[:80]})")
        stop.wait(every)


def start_background(db_path, log=print):
    st = cfg.credits_settings()
    if not st["enabled"]:
        return None
    log(f"credits: watching deposits of the token to {st['treasury'][:6]}…{st['treasury'][-4:]}")
    t = threading.Thread(target=worker, args=(db_path, log), daemon=True)
    t.start()
    return t
