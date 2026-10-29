"""Backfill / follow loop: fetch logs, decode, pair trades, store."""
import time

import requests

from . import config as cfg
from .decode import (Log, decode_curve, decode_launch, decode_router, pair_trades)
from .rpc import RangeTooLarge, RpcError, redact


class Indexer:
    MAX_CHUNK = 100000
    RETRY_BIGGER_AFTER = 500   # successful chunks before trying a larger block range again
    ERROR_PAUSE = 10.0

    def __init__(self, rpc, db, log=print, chunk=5000):
        self.rpc, self.db, self.log = rpc, db, log
        self.chunk = chunk
        self.max_chunk = self.MAX_CHUNK
        self.ok_streak = 0
        self._halved_max = None
        self._consecutive_errors = 0
        self._size1_failures = 0

    # fetching ---------------------------------------------------------
    def fetch_chunk(self, lo, hi):
        """One eth_getLogs call for all four event signatures. Curve events come from a
        different contract per token, so the filter can't be scoped by address; launch and
        router events from unrelated contracts that happen to share a signature are dropped
        by the address check in ingest(). One call instead of two halves the RPC usage."""
        return self.rpc.get_logs(lo, hi, topics=[[cfg.TOPIC_LAUNCH, cfg.TOPIC_ROUTER,
                                                  cfg.TOPIC_CURVE_BUY, cfg.TOPIC_CURVE_SELL]])

    def _fetch_decimals(self, token):
        """Best-effort decimals() call for a newly launched token. Returns an
        int in 0..36, or None to fall back to 18. Never raises: a failed call
        must not block indexing, and live verification is still needed for
        non-standard tokens."""
        call = getattr(self.rpc, "call", None)
        if call is None:
            return None
        try:
            out = call("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
            if not out or out == "0x":
                return None
            d = int(out, 16)
            return d if 0 <= d <= 36 else None
        except Exception:
            return None

    # decoding + storing -------------------------------------------------
    def ingest(self, logs):
        launches, routers, curves = [], [], []
        malformed = 0
        for lg in logs:
            try:
                t0 = lg.topics[0] if lg.topics else ""
                if lg.address == cfg.FACTORY and t0 == cfg.TOPIC_LAUNCH:
                    d = decode_launch(lg)
                    if d is None:
                        malformed += 1
                    else:
                        d["decimals"] = self._fetch_decimals(d["token"])
                        launches.append(d)
                elif lg.address == cfg.ROUTER and t0 == cfg.TOPIC_ROUTER:
                    d = decode_router(lg)
                    if d is None:
                        malformed += 1
                    else:
                        routers.append(d)
                elif t0 in (cfg.TOPIC_CURVE_BUY, cfg.TOPIC_CURVE_SELL):
                    c = decode_curve(lg)
                    if c:
                        curves.append(c)
                    else:
                        malformed += 1
            except (IndexError, ValueError) as e:
                # M1: a single malformed log must never kill the follow loop.
                malformed += 1
                self.log(f"skipping malformed log {getattr(lg, 'tx', '?')}: {e}")
        trades, unpaired, skipped = pair_trades(routers, curves)
        blocks = {x["block"] for x in launches} | {x["block"] for x in trades}
        ts = self.db.get_block_ts(blocks)
        missing = blocks - set(ts)
        if missing:
            fresh = self.rpc.get_block_timestamps(missing)
            self.db.put_block_ts(fresh)
            ts.update(fresh)
        self.db.save(launches, trades, ts)
        return {"launches": len(launches), "trades": len(trades),
                "unpaired": unpaired, "skipped": dict(skipped), "malformed": malformed}

    # loops ------------------------------------------------------------
    def backfill(self, start, end, chunk=None, verbose=True):
        """Index blocks start..end. The block-range limit the node enforces is learned once
        (self.max_chunk) and remembered, so later calls don't waste requests rediscovering it."""
        lo = start
        size = min(chunk or self.chunk, self.max_chunk)
        totals = {"launches": 0, "trades": 0, "unpaired": 0}
        try:
            cursor = int(self.db.get_state("cursor")) if self.db.get_state("cursor") is not None else None
        except (TypeError, ValueError):
            cursor = None
        while lo <= end:
            hi = min(lo + size - 1, end)
            try:
                logs = self.fetch_chunk(lo, hi)
            except RangeTooLarge:
                if size == 1:
                    # M11: a persistently too-large size-1 range must not spin
                    # silently forever; surface it and let follow() back off.
                    self._size1_failures += 1
                    if self._size1_failures >= 5:
                        self.log("ALERT: block range still too large at size 1 for "
                                 f"blocks {lo}-{hi}; operator attention needed")
                    raise
                size = max(1, size // 2)
                self.max_chunk = size  # don't grow back into the size the node just refused
                self._halved_max = size
                self.ok_streak = 0
                self.log(f"node limit: using chunks of {size} blocks")
                continue
            self._size1_failures = 0
            # M2 (reorg safety): re-ingesting an already-indexed range first
            # deletes that range, so a reorged transaction with a different
            # hash does not leave a second trade row behind. New ranges
            # (lo > cursor) are unaffected.
            if cursor is not None and lo <= cursor and hasattr(self.db, "delete_blocks"):
                self.db.delete_blocks(lo, hi)
            st = self.ingest(logs)
            for k in totals:
                totals[k] += st[k]
            self.db.set_state("cursor", hi)
            if verbose or st["launches"] or st["trades"] or st["unpaired"]:
                self.log(f"blocks {lo}-{hi}: +{st['launches']} launches, +{st['trades']} trades "
                         f"(total {totals['launches']}/{totals['trades']})")
            lo = hi + 1
            # Retry a bigger size rarely, but do not immediately grow back
            # past a size the node just halved to: only re-probe after a
            # long healthy streak, and only one doubling at a time.
            self.ok_streak += 1
            if self.ok_streak >= self.RETRY_BIGGER_AFTER and self.max_chunk < self.MAX_CHUNK:
                self.max_chunk = min(self.max_chunk * 2, self.MAX_CHUNK)
                self.ok_streak = 0
            size = min(size * 2, self.max_chunk)
        self.chunk = size
        return totals

    def follow(self, poll=3.0, confirmations=1, max_iterations=None, sleep=time.sleep):
        """Keep indexing new blocks. Network errors are logged (keys redacted) and retried,
        so a hosted service doesn't crash on a hiccup. max_iterations is for tests.

        Confirmation lag (M2): only blocks at least `confirmations` behind the
        chain tip are indexed, so a shallow reorg cannot leave confirmed
        trades behind. Production entry points should pass a larger value
        (see cli: 12); the default of 1 is kept for unit tests.
        """
        cursor = self.db.get_state("cursor")
        nxt = int(cursor) + 1 if cursor is not None else None
        i = 0
        while max_iterations is None or i < max_iterations:
            i += 1
            try:
                head = self.rpc.block_number() - confirmations
                if nxt is None:
                    # M3: never silently skip history. This is an explicit,
                    # prominent warning, not a silent default: history before
                    # this block will NOT be indexed unless backfill is run.
                    self.log("WARNING: no cursor found; following from the chain tip. "
                             "History before this block will NOT be indexed unless "
                             "you run backfill first (see backfill --from-block).")
                    nxt = head + 1
                    self.log(f"following from block {nxt}")
                elif i == 1:
                    self.log(f"following from block {nxt}")
                if head >= nxt:
                    self.backfill(nxt, head, verbose=False)
                    nxt = head + 1
                self._consecutive_errors = 0
            except (RpcError, requests.RequestException, IndexError, ValueError) as e:
                self._consecutive_errors += 1
                pause = self.ERROR_PAUSE
                if self._consecutive_errors >= 5:
                    pause = 60.0
                    self.log(f"ALERT: {self._consecutive_errors} consecutive follow errors; "
                             f"backing off for {pause:.0f}s: {redact(e)}")
                else:
                    self.log(f"rpc error, retrying in {pause:.0f}s: {redact(e)}")
                sleep(pause)
                continue
            sleep(poll)
