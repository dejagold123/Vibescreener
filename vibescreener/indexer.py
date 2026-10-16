"""Backfill / follow loop: fetch logs, decode, pair trades, store."""
import time

from . import config as cfg
from .decode import (Log, decode_curve, decode_launch, decode_router, pair_trades)
from .rpc import RangeTooLarge


class Indexer:
    def __init__(self, rpc, db, log=print):
        self.rpc, self.db, self.log = rpc, db, log

    # fetching ---------------------------------------------------------
    def fetch_chunk(self, lo, hi):
        """Two filters: launches+router trades (address-scoped) and per-token
        curve events (topic-scoped, since each token has its own curve)."""
        a = self.rpc.get_logs(lo, hi, address=[cfg.FACTORY, cfg.ROUTER],
                              topics=[[cfg.TOPIC_LAUNCH, cfg.TOPIC_ROUTER]])
        b = self.rpc.get_logs(lo, hi, topics=[[cfg.TOPIC_CURVE_BUY, cfg.TOPIC_CURVE_SELL]])
        return a + b

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
    def backfill(self, start, end, chunk=5000, max_chunk=100000):
        lo, size = start, chunk
        totals = {"launches": 0, "trades": 0, "unpaired": 0}
        while lo <= end:
            hi = min(lo + size - 1, end)
            try:
                logs = self.fetch_chunk(lo, hi)
            except RangeTooLarge:
                if size == 1:
                    raise
                size = max(1, size // 2)
                max_chunk = size  # don't grow back into the size the node just refused
                self.log(f"range too large, shrinking chunk to {size} blocks")
                continue
            st = self.ingest(logs)
            for k in totals:
                totals[k] += st[k]
            self.db.set_state("cursor", hi)
            self.log(f"blocks {lo}-{hi}: +{st['launches']} launches, +{st['trades']} trades "
                     f"(total {totals['launches']}/{totals['trades']})")
            lo = hi + 1
            size = min(size * 2, max_chunk)
        return totals

    def follow(self, poll=3.0, confirmations=1, chunk=2000):
        cursor = self.db.get_state("cursor")
        nxt = int(cursor) + 1 if cursor is not None else self.rpc.block_number()
        self.log(f"following from block {nxt}")
        while True:
            head = self.rpc.block_number() - confirmations
            if head >= nxt:
                self.backfill(nxt, head, chunk=chunk)
                nxt = head + 1
            time.sleep(poll)
