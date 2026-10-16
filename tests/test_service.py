"""Tests for the deployment side: env config, key redaction, follow-mode behaviour,
and the explorer launch backfill. Network access is faked throughout."""
import json
import os
import tempfile
import unittest
from unittest import mock

import requests

from test_decode import LAUNCH, TRADER, URI, launch_data, t_addr, word  # noqa: F401
from vibescreener import cli
from vibescreener import config as cfg
from vibescreener.db import DB
from vibescreener.decode import Log
from vibescreener.explorer import Explorer, ExplorerError, backfill_launches
from vibescreener.indexer import Indexer
from vibescreener.rpc import RangeTooLarge, Rpc, RpcError, redact

SECRET = "alch_SuperSecretKey123456"
ALCHEMY_URL = f"https://robinhood-testnet.g.alchemy.com/v2/{SECRET}"


class ConfigTests(unittest.TestCase):
    def test_rpc_from_alchemy_key(self):
        self.assertEqual(cfg.resolve_rpc_url({"ALCHEMY_API_KEY": "abc123"}),
                         "https://robinhood-testnet.g.alchemy.com/v2/abc123")

    def test_full_rpc_url_wins_over_key(self):
        env = {"VIBESCREENER_RPC": "https://my.node/rpc", "ALCHEMY_API_KEY": "abc123"}
        self.assertEqual(cfg.resolve_rpc_url(env), "https://my.node/rpc")

    def test_public_rpc_is_the_default(self):
        self.assertEqual(cfg.resolve_rpc_url({}), cfg.PUBLIC_RPC)

    def test_pasted_values_are_cleaned(self):
        # dashboards make it easy to paste a trailing newline or wrap the value in quotes
        self.assertEqual(cfg.resolve_rpc_url({"ALCHEMY_API_KEY": '  "abc123"\n'}),
                         "https://robinhood-testnet.g.alchemy.com/v2/abc123")
        self.assertEqual(cfg.resolve_rpc_url({"ALCHEMY_API_KEY": "   "}), cfg.PUBLIC_RPC)

    def test_db_path_precedence(self):
        self.assertEqual(cfg.resolve_db_path({}), "vibescreener.db")
        self.assertEqual(cfg.resolve_db_path({"RAILWAY_VOLUME_MOUNT_PATH": "/data"}),
                         os.path.join("/data", "vibescreener.db"))
        self.assertEqual(cfg.resolve_db_path({"RAILWAY_VOLUME_MOUNT_PATH": "/data",
                                              "VIBESCREENER_DB": "/x/y.db"}), "/x/y.db")

    def test_warns_only_on_railway_without_storage(self):
        self.assertFalse(cfg.on_railway_without_storage({}))                               # laptop
        self.assertTrue(cfg.on_railway_without_storage({"RAILWAY_SERVICE_NAME": "web"}))   # no volume
        self.assertFalse(cfg.on_railway_without_storage(
            {"RAILWAY_SERVICE_NAME": "web", "RAILWAY_VOLUME_MOUNT_PATH": "/data"}))
        self.assertFalse(cfg.on_railway_without_storage(
            {"RAILWAY_SERVICE_NAME": "web", "VIBESCREENER_DB": "/data/v.db"}))

    def test_poll_seconds(self):
        self.assertEqual(cfg.poll_seconds({}), 3.0)
        self.assertEqual(cfg.poll_seconds({"VIBESCREENER_POLL": "5"}), 5.0)
        self.assertEqual(cfg.poll_seconds({"VIBESCREENER_POLL": "banana"}), 3.0)
        self.assertEqual(cfg.poll_seconds({"VIBESCREENER_POLL": "0"}), 0.5)  # never hammer the node

    def test_db_creates_missing_folder(self):
        path = os.path.join(tempfile.mkdtemp(), "volume", "sub", "v.db")
        DB(path)
        self.assertTrue(os.path.exists(path))


class RedactionTests(unittest.TestCase):
    def test_redact_masks_key_in_url(self):
        out = redact(f"HTTPSConnectionPool: 403 for url: {ALCHEMY_URL}")
        self.assertNotIn(SECRET, out)
        self.assertIn("/v2/***", out)

    def test_rpc_error_message_is_redacted(self):
        self.assertNotIn(SECRET, str(RpcError(f"failed {ALCHEMY_URL}")))
        self.assertNotIn(SECRET, str(RangeTooLarge(f"too big {ALCHEMY_URL}")))

    def test_network_error_text_never_contains_the_key(self):
        rpc = Rpc(ALCHEMY_URL, retries=1)
        boom = requests.ConnectionError(f"Max retries exceeded with url: {ALCHEMY_URL}")
        with mock.patch.object(rpc.s, "post", side_effect=boom), mock.patch("time.sleep"):
            with self.assertRaises(RpcError) as ctx:
                rpc.block_number()
        self.assertNotIn(SECRET, str(ctx.exception))


class FakeNodeRpc:
    """Pretends to be a node that refuses ranges over `limit` blocks."""

    def __init__(self, limit=10, head=10_000):
        self.limit, self.head, self.failures, self.calls = limit, head, 0, 0
        self.fail_head_once = False

    def get_logs(self, lo, hi, address=None, topics=None):
        self.calls += 1
        if hi - lo + 1 > self.limit:
            self.failures += 1
            raise RangeTooLarge("block range too large")
        return []

    def block_number(self):
        if self.fail_head_once:
            self.fail_head_once = False
            raise RpcError(f"http 503 from {ALCHEMY_URL}")
        return self.head

    def get_block_timestamps(self, blocks):
        return {b: 1 for b in blocks}


class FollowTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.lines = []
        self.rpc = FakeNodeRpc(limit=10)
        self.idx = Indexer(self.rpc, self.db, log=self.lines.append)

    def test_learned_limit_is_remembered_between_polls(self):
        self.idx.backfill(1, 200, verbose=False)
        first = self.rpc.failures
        self.assertGreater(first, 0)                    # had to discover the limit once
        self.idx.backfill(201, 400, verbose=False)
        self.assertEqual(self.rpc.failures, first)      # and never pays for it again
        self.assertLessEqual(self.idx.max_chunk, 10)

    def test_limit_is_retried_larger_after_a_long_healthy_streak(self):
        self.idx.RETRY_BIGGER_AFTER = 3
        self.idx.backfill(1, 400, verbose=False)
        self.assertGreater(self.rpc.failures, 1)        # re-probed, e.g. after a transient timeout

    def test_quiet_follow_logs_nothing_when_nothing_happens(self):
        self.db.set_state("cursor", 9_900)
        self.idx.follow(max_iterations=2, sleep=lambda s: None)
        self.assertTrue(all("following from" in l or "node limit" in l for l in self.lines), self.lines)
        self.assertEqual(self.db.get_state("cursor"), str(self.rpc.head - 1))

    def test_follow_survives_an_rpc_error_without_leaking_the_key(self):
        self.db.set_state("cursor", 9_980)
        self.rpc.fail_head_once = True
        slept = []
        self.idx.follow(max_iterations=3, sleep=slept.append)
        text = "\n".join(self.lines)
        self.assertIn("rpc error, retrying", text)
        self.assertNotIn(SECRET, text)
        self.assertIn(Indexer.ERROR_PAUSE, slept)                   # paused, then carried on
        self.assertEqual(self.db.get_state("cursor"), str(self.rpc.head - 1))

    def test_fresh_database_starts_at_the_chain_tip(self):
        self.idx.follow(max_iterations=1, sleep=lambda s: None)
        self.assertIsNone(self.db.get_state("cursor"))              # nothing indexed yet
        self.assertTrue(any(f"from block {self.rpc.head}" in l for l in self.lines), self.lines)


# --- explorer -------------------------------------------------------------------------------------
LAUNCH_TS = "2026-10-16T00:19:05.000000Z"


def item_from(lg, ts=LAUNCH_TS):
    return {"topics": list(lg.topics) + [None] * (4 - len(lg.topics)), "data": lg.data,
            "block_number": lg.block, "index": lg.log_index, "transaction_hash": lg.tx,
            "block_timestamp": ts}


def make_launch(n, quote=cfg.ZERO):
    token = "0x" + f"{0xA000 + n:040x}"
    curve = "0x" + f"{0xB000 + n:040x}"
    return Log(cfg.FACTORY, (cfg.TOPIC_LAUNCH, "0x" + f"{n:064x}", t_addr(TRADER), t_addr(token)),
               launch_data(curve, quote, TRADER, f"Token{n}", f"T{n}", URI),
               136_000_000 + n, 5, "0x" + f"{n:064x}")


FEE_EVENT = {"topics": ["0x5e4996fc710ab2bf894de604570b39df292e3629ba35a424cd622f8da787e537", None, None, None],
             "data": "0x00", "block_number": 1, "index": 1, "transaction_hash": "0x" + "1" * 64,
             "block_timestamp": LAUNCH_TS}


class Resp:
    def __init__(self, payload=None, status=200, headers=None):
        self.status_code, self._payload, self.headers = status, payload, headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def get(self, url, params=None, timeout=None):
        self.requests.append((url, params))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def explorer_with(responses):
    sleeps = []
    ex = Explorer(session=FakeSession(responses), sleep=sleeps.append)
    return ex, sleeps


class LaunchBackfillTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.page1 = {"items": [item_from(make_launch(1, cfg.VIBEVIBE_TOKEN)), FEE_EVENT, item_from(make_launch(2))],
                      "next_page_params": {"block_number": 136_000_001, "index": 5, "items_count": 50}}
        self.page2 = {"items": [item_from(LAUNCH), FEE_EVENT], "next_page_params": None}

    def test_crawls_all_pages_and_stores_launches(self):
        ex, _ = explorer_with([Resp(self.page1), Resp(self.page2)])
        res = backfill_launches(ex, self.db, log=lambda *_: None)
        self.assertEqual(res, {"pages": 2, "launches": 3, "skipped": 0, "done": True})
        rows = self.db.q("SELECT symbol, quote_token, ts FROM tokens ORDER BY symbol")
        self.assertEqual([r["symbol"] for r in rows], ["JHJ", "T1", "T2"])
        quotes = {r["symbol"]: r["quote_token"] for r in rows}
        self.assertEqual(quotes["T1"], cfg.VIBEVIBE_TOKEN)      # the point of all this: quote tokens known
        self.assertEqual(quotes["T2"], cfg.ZERO)
        self.assertIsNotNone(rows[0]["ts"])                     # timestamp taken from the explorer
        # the second request carried the cursor the first page returned
        self.assertEqual(ex.s.requests[1][1], self.page1["next_page_params"])
        self.assertIn(f"/api/v2/addresses/{cfg.FACTORY}/logs", ex.s.requests[0][0])

    def test_finished_crawl_is_not_repeated(self):
        ex, _ = explorer_with([Resp(self.page1), Resp(self.page2)])
        backfill_launches(ex, self.db, log=lambda *_: None)
        ex2, _ = explorer_with([])                               # any request would blow up
        self.assertTrue(backfill_launches(ex2, self.db, log=lambda *_: None)["done"])
        self.assertEqual(ex2.s.requests, [])

    def test_resumes_from_the_saved_cursor(self):
        ex, _ = explorer_with([Resp(self.page1)])
        res = backfill_launches(ex, self.db, log=lambda *_: None, max_pages=1)   # "crash" after page 1
        self.assertFalse(res["done"])
        ex2, _ = explorer_with([Resp(self.page2)])
        res2 = backfill_launches(ex2, self.db, log=lambda *_: None)
        self.assertTrue(res2["done"])
        self.assertEqual(ex2.s.requests[0][1], self.page1["next_page_params"])   # picked up where it stopped
        self.assertEqual(self.db.q("SELECT COUNT(*) n FROM tokens")[0]["n"], 3)

    def test_restart_flag_crawls_again(self):
        ex, _ = explorer_with([Resp(self.page2)])
        backfill_launches(ex, self.db, log=lambda *_: None)
        ex2, _ = explorer_with([Resp(self.page2)])
        res = backfill_launches(ex2, self.db, log=lambda *_: None, restart=True)
        self.assertEqual(res["pages"], 1)
        self.assertEqual(ex2.s.requests[0][1], None)

    def test_rate_limit_is_retried_using_retry_after(self):
        ex, sleeps = explorer_with([Resp(None, 429, {"Retry-After": "7"}), Resp(self.page2)])
        backfill_launches(ex, self.db, log=lambda *_: None)
        self.assertIn(7.0, sleeps)
        self.assertEqual(self.db.q("SELECT COUNT(*) n FROM tokens")[0]["n"], 1)

    def test_network_errors_are_retried_then_reported(self):
        ex, _ = explorer_with([requests.ConnectionError("x")] * 6)
        with self.assertRaises(ExplorerError):
            backfill_launches(ex, self.db, log=lambda *_: None)

    def test_hard_http_error_fails_fast(self):
        ex, _ = explorer_with([Resp(None, 404)])
        with self.assertRaises(ExplorerError):
            backfill_launches(ex, self.db, log=lambda *_: None)

    def test_malformed_items_are_skipped_not_fatal(self):
        bad = dict(item_from(make_launch(9)))
        del bad["transaction_hash"]
        ex, _ = explorer_with([Resp({"items": [bad, item_from(make_launch(3))], "next_page_params": None})])
        res = backfill_launches(ex, self.db, log=lambda *_: None)
        self.assertEqual((res["launches"], res["skipped"]), (1, 1))


class RunServiceTests(unittest.TestCase):
    def test_explorer_failure_does_not_stop_indexing(self):
        db, lines = DB(":memory:"), []
        broken, _ = explorer_with([Resp(None, 404)])
        with mock.patch.object(Indexer, "follow") as follow:
            cli.run_service(FakeNodeRpc(), db, poll=1, explorer=broken, log=lines.append)
        follow.assert_called_once()
        self.assertTrue(any("launch backfill paused" in l for l in lines), lines)

    def test_service_logs_never_contain_the_rpc_key(self):
        db, lines = DB(":memory:"), []
        done, _ = explorer_with([Resp({"items": [], "next_page_params": None})])
        with mock.patch.object(cfg, "RPC_URL", ALCHEMY_URL), mock.patch.object(Indexer, "follow"):
            cli.run_service(FakeNodeRpc(), db, explorer=done, log=lines.append)
        self.assertNotIn(SECRET, "\n".join(lines))
        self.assertTrue(any("alchemy endpoint" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
