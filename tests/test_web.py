"""Dashboard and API tests against a seeded temporary database."""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request

from vibescreener import config as cfg
from vibescreener import web
from vibescreener.db import DB

W = 10 ** 18
TOK_A = "0x" + "a1" * 20
TOK_V = "0x" + "b2" * 20
TOK_X = "0x" + "c3" * 20   # trades but no launch row
ALICE = "0x" + "11" * 20
BOB = "0x" + "22" * 20
CREATOR = "0x" + "33" * 20
EVIL = '<img src=x onerror=alert(1)>'


def launch(token, symbol, quote, ts, block):
    return dict(token=token, curve="0x" + "dd" * 20, creator=CREATOR, name=symbol + " coin", symbol=symbol,
                uri="", quote_token=quote, launch_id=1, block=block, tx="0x" + "e" * 64, log_index=0, extra={})


def trade(n, side, trader, token, tok, quote, block):
    return dict(tx=f"0x{n:064x}", log_index=n, block=block, side=side, trader=trader, token=token,
                curve="0x" + "dd" * 20, token_amount=int(tok * W), token_fee=0, quote_amount=int(quote * W),
                quote_fee=0, reserve_after=0, native_in=0)


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.path = os.path.join(cls.dir, "t.db")
        db = DB(cls.path)
        now = int(time.time())
        ts = {b: now - 600 + b for b in range(1, 20)}
        db.save([launch(TOK_A, "AAA", cfg.ZERO, now, 1), launch(TOK_V, EVIL, cfg.VIBEVIBE_TOKEN, now, 2)], [
            trade(1, "buy", ALICE, TOK_A, 100, 1.0, 3),
            trade(2, "sell", ALICE, TOK_A, 100, 1.5, 4),      # alice +0.5 ETH, complete
            trade(3, "sell", BOB, TOK_A, 50, 0.4, 5),         # bob never seen buying -> excluded
            trade(4, "buy", BOB, TOK_V, 10, 2.0, 6),
            trade(5, "buy", BOB, TOK_X, 10, 2.0, 7),          # token with no launch row
        ], ts)
        cls.server = web.serve(cls.path, 0, host="127.0.0.1")
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def j(self, path):
        status, body = self.get(path)
        return status, json.loads(body)

    def test_overview_counts_and_exclusions(self):
        web._cache.clear()
        s, o = self.j("/api/overview?hours=24")
        self.assertEqual(s, 200)
        self.assertEqual((o["tokens"], o["trades"], o["trades_window"], o["wallets_window"]), (2, 5, 5, 2))
        quotes = {v["quote"]: v["volume"] for v in o["volume_window"]}
        self.assertAlmostEqual(quotes["ETH"], 2.9)             # 1.0 + 1.5 + 0.4, never mixed with VIBEVIBE
        self.assertAlmostEqual(quotes["VIBEVIBE"], 2.0)
        self.assertAlmostEqual(quotes["unknown-quote"], 2.0)
        self.assertEqual(o["pnl_excluded"]["incomplete_history"], 1)

    def test_wallet_leaderboard_only_complete_positions(self):
        web._cache.clear()
        s, w = self.j("/api/wallets")
        pnl = {(r["trader"], r["quote"]): r["pnl_quote"] for r in w}
        self.assertAlmostEqual(pnl[(ALICE, "ETH")], 0.5)
        self.assertNotIn((BOB, "ETH"), pnl)

    def test_token_and_wallet_detail(self):
        s, t = self.j(f"/api/token/{TOK_A}")
        self.assertEqual((s, t["symbol"], t["quote"], t["trades"]), (200, "AAA", "ETH", 3))
        self.assertEqual(len(t["recent_trades"]), 3)
        prices = [p["price"] for p in t["price_series"]]
        self.assertEqual(prices, [0.01, 0.015, 0.008])         # oldest to newest
        s, w = self.j(f"/api/wallet/{BOB}")
        pos = {p["token"]: p["pnl"] for p in w["positions"]}
        self.assertIsNone(pos[TOK_A])                          # missing history -> no PnL claimed

    def test_bad_input(self):
        self.assertEqual(self.get("/api/token/notanaddress")[0], 400)
        self.assertEqual(self.get("/api/token/0x" + "9" * 40)[0], 404)
        self.assertEqual(self.get("/api/token/0x1%27%20OR%201=1--")[0], 400)
        self.assertEqual(self.get("/api/nope")[0], 404)
        s, f = self.j("/api/feed?limit=banana")
        self.assertEqual(s, 200)

    def test_feed_has_launches_and_trades(self):
        s, f = self.j("/api/feed?limit=50")
        self.assertEqual({i["kind"] for i in f}, {"trade", "launch"})

    def test_page_and_health(self):
        s, body = self.get("/")
        self.assertEqual(s, 200)
        self.assertIn(b"VibeScreener", body)
        self.assertEqual(self.get("/health"), (200, b"ok"))

    def test_page_escapes_chain_text(self):
        # the hostile symbol is stored verbatim; the page must run it through esc()
        s, f = self.j(f"/api/token/{TOK_V}")
        self.assertEqual(f["symbol"], EVIL)
        page = self.get("/")[1].decode()
        self.assertNotIn("innerHTML=" + "`" + "${t.symbol", page)
        self.assertIn("const esc=", page)

    def test_writes_are_impossible(self):
        db = web.ReadDB(self.path)
        with self.assertRaises(Exception):
            db.conn.execute("DELETE FROM trades")
        db.close()

    def test_empty_database_is_not_a_crash(self):
        p = os.path.join(self.dir, "empty.db")
        DB(p)
        web._cache.clear()
        s, o = web.route(p, "/api/overview", {})
        self.assertEqual((s, o["trades"], o["last_trade_ts"]), (200, 0, None))
        self.assertEqual(web.route(p, "/api/wallets", {})[1], [])

    def test_port_selection(self):
        self.assertEqual(cfg.web_port({}), 8000)
        self.assertEqual(cfg.web_port({"PORT": "8080"}), 8080)
        self.assertEqual(cfg.web_port({"PORT": "8080", "VIBESCREENER_PORT": "9000"}), 9000)
        self.assertEqual(cfg.web_port({"PORT": "junk"}), 8000)

    def test_port_in_use_does_not_stop_indexing(self):
        logs = []
        self.assertIsNone(web.start_background(self.path, self.port, log=logs.append))
        self.assertTrue(any("not started" in l for l in logs))


if __name__ == "__main__":
    unittest.main()
