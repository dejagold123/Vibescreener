"""Backfill token launches from the Blockscout explorer API.

Why: eth_getLogs on a free RPC plan only allows a handful of blocks per call, so reading
months of history that way takes weeks. The explorer serves the factory's logs page by page
with no block-range limit, which is enough to learn every token's name, creator, curve and
quote token. Trades are NOT backfilled this way (that would be millions of logs); they are
indexed from the chain tip onward, and PnL ignores positions whose buys predate that.

The crawl is resumable: the explorer's page cursor is stored in the database after every
page, so a restart carries on instead of starting over.
"""
import datetime
import json
import time

import requests

from . import config as cfg
from .decode import Log, decode_launch

CURSOR_KEY = "launch_cursor"
DONE_KEY = "launches_done"


class ExplorerError(Exception):
    pass


class Explorer:
    def __init__(self, base=cfg.EXPLORER, session=None, sleep=time.sleep, retries=6):
        self.base = base.rstrip("/")
        self.s = session or requests.Session()
        self.sleep = sleep
        self.retries = retries

    def get(self, path, params=None):
        last = "no attempt"
        for attempt in range(self.retries):
            try:
                r = self.s.get(self.base + path, params=params, timeout=30)
            except requests.RequestException as e:
                last = f"{type(e).__name__}"
                self.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"http {r.status_code}"
                try:
                    wait = float(r.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    wait = min(2 ** attempt, 30)
                self.sleep(wait)
                continue
            if r.status_code != 200:
                raise ExplorerError(f"explorer returned http {r.status_code} for {path}")
            try:
                return r.json()
            except ValueError:
                last = "invalid json"
                self.sleep(min(2 ** attempt, 30))
        raise ExplorerError(f"explorer request failed after {self.retries} tries ({last})")


def _epoch(iso):
    if not iso:
        return None
    return int(datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _launch_from_item(item):
    """Blockscout log item -> decoded launch, or None if it isn't a launch event."""
    topics = [t.lower() for t in (item.get("topics") or []) if t]
    if not topics or topics[0] != cfg.TOPIC_LAUNCH or len(topics) < 4:
        return None, None
    lg = Log(address=cfg.FACTORY, topics=tuple(topics), data=(item.get("data") or "0x").lower(),
             block=int(item["block_number"]), log_index=int(item["index"]),
             tx=item["transaction_hash"].lower())
    return decode_launch(lg), _epoch(item.get("block_timestamp"))


def backfill_launches(explorer, db, log=print, max_pages=None, restart=False):
    """Crawl the factory's logs newest-first and store every launch. Returns a summary.
    Safe to call repeatedly: finished crawls return immediately unless restart=True."""
    if restart:
        db.set_state(CURSOR_KEY, "")
        db.set_state(DONE_KEY, "")
    if db.get_state(DONE_KEY):
        return {"pages": 0, "launches": 0, "skipped": 0, "done": True}

    raw = db.get_state(CURSOR_KEY)
    params = json.loads(raw) if raw else None
    pages = launches = skipped = 0
    while max_pages is None or pages < max_pages:
        data = explorer.get(f"/api/v2/addresses/{cfg.FACTORY}/logs", params)
        batch, ts = [], {}
        for item in data.get("items") or []:
            try:
                launch, when = _launch_from_item(item)
            except (KeyError, ValueError, IndexError):
                skipped += 1
                continue
            if launch:
                batch.append(launch)
                if when:
                    ts[launch["block"]] = when
        db.save(batch, [], ts)
        pages += 1
        launches += len(batch)
        params = data.get("next_page_params")
        if params:
            db.set_state(CURSOR_KEY, json.dumps(params))
        else:
            db.set_state(CURSOR_KEY, "")
            db.set_state(DONE_KEY, "1")
        if pages % 20 == 0 or not params:
            log(f"launch backfill: {pages} pages, {launches} launches stored")
        if not params:
            return {"pages": pages, "launches": launches, "skipped": skipped, "done": True}
        explorer.sleep(0.2)  # be polite to the public explorer
    return {"pages": pages, "launches": launches, "skipped": skipped, "done": False}
