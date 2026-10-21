"""Token metadata: parsing untrusted JSON, safe fetching, retry bookkeeping, and how it reaches the API."""
import json
import os
import tempfile
import time
import unittest

from test_web import TOK_A, launch, W, trade, ALICE
from vibescreener import config as cfg
from vibescreener import meta, web
from vibescreener.db import DB

CID1 = "bafkreicsmbhmueyqxp4q2zcrehdprfdznl4o4335r5mjiukb5oqbcdqgxa"
IMG = "bafkreianmbtmwt4qriwlskr4goufifqw44ylghhhyicpwbx3aggkrfatau"
# the real metadata file of the JHJ token, as served on IPFS
REAL = json.dumps({"chainId": 46630, "creator": "0x03811ed81bcadded400d8e9fa7692bce8ca413b1", "description": "",
                   "image": {"bytes": 39014, "height": 1536, "mimeType": "image/webp",
                             "uri": "ipfs://" + IMG, "width": 686},
                   "name": "Jubberish", "projectTypes": ["meme_artcoins"], "schemaVersion": 1,
                   "socials": {}, "symbol": "JHJ"})


class Resp:
    def __init__(self, status=200, body=b""):
        self.status_code, self._body = status, body
        self.raw = self

    def read(self, n, decode_content=True):
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    def __init__(self, responder):
        self.responder, self.calls = responder, []

    def get(self, url, **kw):
        self.calls.append(url)
        r = self.responder(url)
        if isinstance(r, Exception):
            raise r
        return r


class ParseTests(unittest.TestCase):
    def test_real_metadata(self):
        p = meta.parse(REAL)
        self.assertEqual((p["image_cid"], p["description"], p["socials"]), (IMG, "", {}))

    def test_image_as_plain_string_and_socials_filtered(self):
        raw = json.dumps({"image": "ipfs://" + IMG, "description": "x" * 900,
                          "socials": {"x": "https://x.com/a", "evil": "javascript:alert(1)", "n": 5}})
        p = meta.parse(raw)
        self.assertEqual(p["image_cid"], IMG)
        self.assertEqual(len(p["description"]), 500)
        self.assertEqual(p["socials"], {"x": "https://x.com/a"})

    def test_only_ipfs_content_ids_are_accepted(self):
        for bad in ("http://evil.com/a.png", "ipfs://../etc/passwd", "data:text/html,hi", "", None, 7,
                    "ipfs://short", "ipfs://" + CID1[:-1] + "!"):
            self.assertIsNone(meta.cid_from_uri(bad), bad)
        self.assertEqual(meta.cid_from_uri("ipfs://" + CID1), CID1)
        self.assertEqual(meta.cid_from_uri("ipfs://ipfs/" + CID1), CID1)
        self.assertEqual(meta.cid_from_uri("ipfs://" + CID1 + "/../x?y"), CID1)   # only the id is ever used
        self.assertIsNone(meta.parse(json.dumps({"image": "https://evil.com/x.png"}))["image_cid"])

    def test_junk_json(self):
        for raw in (b"not json", b"[1,2]", b"null", "{", None):
            self.assertIsNone(meta.parse(raw))


class FetchTests(unittest.TestCase):
    GW = ["https://a/ipfs/", "https://b/ipfs/"]

    def test_falls_through_to_next_gateway(self):
        s = FakeSession(lambda u: Resp(503) if u.startswith("https://a") else Resp(200, b"{}"))
        self.assertEqual(meta.fetch_json(CID1, s, self.GW), b"{}")
        self.assertEqual(len(s.calls), 2)

    def test_all_gateways_down_raises(self):
        s = FakeSession(lambda u: Resp(404))
        with self.assertRaises(RuntimeError):
            meta.fetch_json(CID1, s, self.GW)

    def test_oversized_response_rejected(self):
        s = FakeSession(lambda u: Resp(200, b"x" * (meta.MAX_BYTES + 5)))
        with self.assertRaises(ValueError):
            meta.fetch_json(CID1, s, self.GW)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = DB(os.path.join(self.dir, "m.db"))
        L = launch(TOK_A, "AAA", cfg.ZERO, 0, 1)
        L["uri"] = "ipfs://" + CID1
        self.db.save([L], [trade(1, "buy", ALICE, TOK_A, 10, 1, 2)], {1: int(time.time()), 2: int(time.time())})
        self.row = {"token": TOK_A, "uri": "ipfs://" + CID1}

    def status(self):
        return self.db.q("SELECT * FROM token_meta WHERE token = ?", (TOK_A,))[0]

    def test_success_is_stored_and_not_refetched(self):
        s = FakeSession(lambda u: Resp(200, REAL.encode()))
        self.assertEqual(meta.process(self.db, s, self.row, gateways=["https://a/"]), "ok")
        self.assertEqual(self.status()["image_cid"], IMG)
        self.assertEqual(meta.pending(self.db), [])

    def test_failure_retries_later_then_gives_up(self):
        s = FakeSession(lambda u: Resp(500))
        t0 = 1_000_000
        for n in range(1, meta.GIVE_UP_AFTER):
            self.assertEqual(meta.process(self.db, s, self.row, now=t0, gateways=["https://a/"]), "retry")
            self.assertEqual(self.status()["next_try"], t0 + meta.RETRY_AFTER[n - 1])
            self.assertEqual(meta.pending(self.db, now=t0), [])                       # not yet due
            self.assertEqual(len(meta.pending(self.db, now=t0 + 10 ** 6)), 1)         # due later
        self.assertEqual(meta.process(self.db, s, self.row, now=t0, gateways=["https://a/"]), "failed")
        self.assertEqual(meta.pending(self.db, now=t0 + 10 ** 9), [])

    def test_unusable_metadata_is_not_retried(self):
        s = FakeSession(lambda u: Resp(200, b"<html>nope</html>"))
        self.assertEqual(meta.process(self.db, s, self.row, gateways=["https://a/"]), "bad")
        self.assertEqual(meta.pending(self.db, now=time.time() + 10 ** 9), [])

    def test_non_ipfs_uri_is_never_fetched(self):
        s = FakeSession(lambda u: Resp(200, REAL.encode()))
        row = {"token": TOK_A, "uri": "http://169.254.169.254/latest/meta-data"}
        self.assertEqual(meta.process(self.db, s, row), "bad")
        self.assertEqual(s.calls, [])

    def test_api_exposes_image_urls_only_for_valid_ids(self):
        s = FakeSession(lambda u: Resp(200, REAL.encode()))
        meta.process(self.db, s, self.row, gateways=["https://a/"])
        db = web.ReadDB(os.path.join(self.dir, "m.db"))
        t = web.token_detail(db, TOK_A)
        self.assertEqual(t["image_urls"], [g + IMG for g in cfg.IPFS_GATEWAYS])
        self.assertNotIn("image_cid", t)
        self.db.conn.execute("UPDATE token_meta SET image_cid = '../../evil'")
        self.db.conn.commit()
        self.assertEqual(web.token_detail(db, TOK_A)["image_urls"], [])
        db.close()

    def test_worker_keeps_going_when_a_batch_explodes(self):
        import threading
        stop, logs, calls = threading.Event(), [], []

        def boom(*a, **k):
            calls.append(1)
            stop.set()
            raise RuntimeError("db locked")

        orig = meta.run_batch
        meta.run_batch = boom
        try:
            meta.worker(os.path.join(self.dir, "m.db"), log=logs.append, stop=stop)
        finally:
            meta.run_batch = orig
        self.assertTrue(any("paused" in l for l in logs))


if __name__ == "__main__":
    unittest.main()
