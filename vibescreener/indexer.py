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

    # fetching ---------------------------------------------------------
    def fetch_chunk(self, lo, hi):
        """One eth_getLogs call for all four event signatures. Curve events come from a
        different contract per token, so the filter can't be scoped by address; launch and
        router events from unrelated contracts that happen to share a signature are dropped
        by the address check in ingest(). One call instead of two halves the RPC usage."""
        return self.rpc.get_logs(lo, hi, topics=[[cfg.TOPIC_LAUNCH, cfg.TOPIC_ROUTER,
                                                  cfg.TOPIC_CURVE_BUY, cfg.TOPIC_CURVE_SELL]])

    # decoding + storing -------------------------------------------------
    def ingest(self, logs):
        launches, routers, curves = [], [], []
        for lg in logs:
            t0 = lg.topics[0] if lg.topics else ""
            if lg.address == cfg.FACTORY and t0 == cfg.TOPIC_LAUNCH:
                launches.append(decode_launch(lg))
            elif lg.address == cfg.ROUTER and t0 == cfg.TOPIC_ROUTER:
                routers.append(decode_router(lg))
            elif t0 in (cfg.TOPIC_CURVE_BUY, cfg.TOPIC_CURVE_SELL):
                c = decode_curve(lg)
                if c:
                    curves.append(c)
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
                "unpaired": unpaired, "skipped": dict(skipped)}

    # loops ------------------------------------------------------------
    def backfill(self, start, end, chunk=None, verbose=True):
        """Index blocks start..end. The block-range limit the node enforces is learned once
        (self.max_chunk) and remembered, so later calls don't waste requests rediscovering it."""
        lo = start
        size = min(chunk or self.chunk, self.max_chunk)
        totals = {"launches": 0, "trades": 0, "unpaired": 0}
        while lo <= end:
            hi = min(lo + size - 1, end)
            try:
                logs = self.fetch_chunk(lo, hi)
            except RangeTooLarge:
                if size == 1:
                    raise
                size = max(1, size // 2)
                self.max_chunk = size  # don't grow back into the size the node just refused
                self.ok_streak = 0
                self.log(f"node limit: using chunks of {size} blocks")
                continue
            st = self.ingest(logs)
            for k in totals:
                totals[k] += st[k]
            self.db.set_state("cursor", hi)
            if verbose or st["launches"] or st["trades"] or st["unpaired"]:
                self.log(f"blocks {lo}-{hi}: +{st['launches']} launches, +{st['trades']} trades "
                         f"(total {totals['launches']}/{totals['trades']})")
            lo = hi + 1
            # An occasional timeout can shrink the limit by mistake; retry a bigger size rarely.
            self.ok_streak += 1
            if self.ok_streak >= self.RETRY_BIGGER_AFTER and self.max_chunk < self.MAX_CHUNK:
                self.max_chunk = min(self.max_chunk * 2, self.MAX_CHUNK)
                self.ok_streak = 0
            size = min(size * 2, self.max_chunk)
        self.chunk = size
        return totals

    def follow(self, poll=3.0, confirmations=1, max_iterations=None, sleep=time.sleep):
        """Keep indexing new blocks. Network errors are logged (keys redacted) and retried,
        so a hosted service doesn't crash on a hiccup. max_iterations is for tests."""
        cursor = self.db.get_state("cursor")
        nxt = int(cursor) + 1 if cursor is not None else None
        i = 0
        while max_iterations is None or i < max_iterations:
            i += 1
            try:
                head = self.rpc.block_number() - confirmations
                if nxt is None:
                    nxt = head + 1
                    self.log(f"following from block {nxt}")
                elif i == 1:
                    self.log(f"following from block {nxt}")
                if head >= nxt:
                    self.backfill(nxt, head, verbose=False)
                    nxt = head + 1
            except (RpcError, requests.RequestException) as e:
                self.log(f"rpc error, retrying in {self.ERROR_PAUSE:.0f}s: {redact(e)}")
                sleep(self.ERROR_PAUSE)
                continue
            sleep(poll)
