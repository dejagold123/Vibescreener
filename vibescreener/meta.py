"""Token metadata (description, image, socials) from each launch's metadata link.

Launch metadata lives on IPFS as a small JSON file. A background worker fetches it
slowly, newest tokens first, and stores the result so the dashboard can show images
and descriptions. Everything fetched is untrusted: only ipfs:// content ids are
fetched (never arbitrary URLs), responses are size-limited, and stored text is
truncated. The browser later loads images straight from the IPFS gateway.
"""
import json
import re
import threading
import time

import requests

from . import config as cfg

CID = re.compile(r"^(?:Qm[1-9A-HJ-NP-Za-km-z]{44}|b[a-z2-7]{50,120})$")
MAX_BYTES = 256 * 1024
RETRY_AFTER = (600, 3600, 6 * 3600, 24 * 3600)   # seconds before attempt 2, 3, 4, 5
GIVE_UP_AFTER = 5


def cid_from_uri(uri):
    """The content id of an ipfs://<cid> link, or None for anything else."""
    if not isinstance(uri, str):
        return None
    m = re.match(r"^ipfs://(?:ipfs/)?([^/?#]+)", uri.strip())
    return m.group(1) if m and CID.match(m.group(1)) else None


def _text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ""


def parse(raw):
    """Pull the fields we show out of untrusted metadata JSON; returns None if unusable."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    image = data.get("image")
    image = image.get("uri") if isinstance(image, dict) else image
    socials = {}
    if isinstance(data.get("socials"), dict):
        for key, val in list(data["socials"].items())[:8]:
            if isinstance(val, str) and re.match(r"^https?://", val.strip(), re.I):
                socials[_text(key, 24)] = val.strip()[:200]
    return {"description": _text(data.get("description"), 500),
            "image_cid": cid_from_uri(image) if isinstance(image, str) else None,
            "socials": socials}


def fetch_json(cid, session, gateways=None, timeout=10):
    """Fetch one metadata file, trying each gateway in turn. Raises on total failure."""
    last = None
    for gw in gateways or cfg.IPFS_GATEWAYS:
        try:
            with session.get(gw + cid, timeout=timeout, stream=True) as r:
                if r.status_code != 200:
                    last = f"http {r.status_code}"
                    continue
                body = r.raw.read(MAX_BYTES + 1, decode_content=True)
                if len(body) > MAX_BYTES:
                    raise ValueError("metadata too large")
                return body
        except (requests.RequestException, OSError) as e:
            last = str(e)[:80]
    raise RuntimeError(last or "no gateway answered")


def pending(db, limit=20, now=None):
    now = int(now or time.time())
    return db.q("""SELECT k.token, k.uri FROM tokens k LEFT JOIN token_meta m ON m.token = k.token
                   WHERE k.uri LIKE 'ipfs://%' AND (m.token IS NULL OR
                         (m.status = 'retry' AND m.next_try <= ?))
                   ORDER BY k.block DESC LIMIT ?""", (now, limit))


def process(db, session, row, now=None, gateways=None):
    """Fetch and store metadata for one token. Returns the status written."""
    now = int(now or time.time())
    cid = cid_from_uri(row["uri"])
    prev = db.q("SELECT attempts FROM token_meta WHERE token = ?", (row["token"],))
    attempts = (prev[0]["attempts"] if prev else 0) + 1
    status, fields = "ok", {"description": "", "image_cid": None, "socials": {}}
    try:
        if not cid:
            raise ValueError("not an ipfs link")
        parsed = parse(fetch_json(cid, session, gateways))
        if parsed is None:
            status = "bad"          # reachable but not usable metadata; retrying will not help
        else:
            fields = parsed
    except ValueError:
        status = "bad"
    except Exception:
        status = "retry" if attempts < GIVE_UP_AFTER else "failed"
    next_try = now + RETRY_AFTER[min(attempts - 1, len(RETRY_AFTER) - 1)] if status == "retry" else None
    with db.conn:
        db.conn.execute("INSERT OR REPLACE INTO token_meta VALUES (?,?,?,?,?,?,?,?)",
                        (row["token"], row["uri"], fields["description"], fields["image_cid"],
                         json.dumps(fields["socials"]), status, attempts, next_try))
    return status


def run_batch(db, session, limit=20, pause=0.5, sleep=time.sleep, gateways=None):
    rows = pending(db, limit)
    for row in rows:
        process(db, session, row, gateways=gateways)
        sleep(pause)
    return len(rows)


def worker(db_path, log=print, idle=30, stop=None, sleep=time.sleep):
    from .db import DB
    db, session = DB(db_path), requests.Session()
    session.headers["User-Agent"] = "VibeScreener/1 (+https://github.com/dejagold123/Vibescreener)"
    stop = stop or threading.Event()
    done = 0
    while not stop.is_set():
        try:
            n = run_batch(db, session, sleep=sleep)
            done += n
            if n:
                log(f"token metadata: {done} processed")
        except Exception as e:      # never let a metadata problem touch indexing
            log(f"token metadata paused ({str(e)[:80]})")
            n = 0
        if not n:
            stop.wait(idle)


def start_background(db_path, log=print):
    if cfg.meta_disabled():
        log("token metadata: disabled")
        return None
    t = threading.Thread(target=worker, args=(db_path, log), daemon=True)
    t.start()
    return t
