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
        self.assertNotIn("unknown-quote", quotes)        # unlike units are counted, never summed
        self.assertEqual(o["other_quote_trades"], 1)
        self.assertEqual(o["pnl_excluded"]["incomplete_history"], 1)

    def test_wallet_leaderboard_only_complete_positions(self):
        web._cache.clear()
        s, w = self.j("/api/wallets")
        pnl = {(r["trader"], r["quote"]): r["pnl_quote"] for r in w}
        self.assertAlmostEqual(pnl[(ALICE, "ETH")], 0.5)
        self.assertNotIn((BOB, "ETH"), pnl)

    def test_wallet_ranking_never_mixes_quote_tokens(self):
        web._cache.clear()
        s, v = self.j("/api/wallets?quote=vibevibe")
        self.assertTrue(v and all(r["quote"] == "VIBEVIBE" for r in v))
        s, e = self.j("/api/wallets?quote=eth")
        self.assertTrue(e and all(r["quote"] == "ETH" for r in e))
        s, d = self.j("/api/wallets?quote=0xdead")       # junk falls back to ETH
        self.assertTrue(all(r["quote"] == "ETH" for r in d))

    def test_token_quoted_in_another_token_is_named_by_symbol(self):
        db = web.ReadDB(self.path)
        rows = web._label([{"quote_token": TOK_A}], db)
        db.close()
        self.assertEqual((rows[0]["quote"], rows[0]["quote_is_token"]), ("AAA", True))

    def test_trending_change_and_pressure(self):
        web._cache.clear()
        db = web.ReadDB(self.path)
        t = {r["token"]: r for r in web.trending(db, 24)}
        a = t[TOK_A]                                    # buy 100@1.0 -> sell 100@1.5 -> sell 50@0.4
        self.assertEqual((a["trades"], a["buys"], a["sells"]), (3, 1, 2))
        self.assertAlmostEqual(a["buy_share"], 1 / 3)
        self.assertAlmostEqual(a["first_price"], 0.01)
        self.assertAlmostEqual(a["last_price"], 0.008)
        self.assertAlmostEqual(a["change_pct"], -20.0)
        self.assertAlmostEqual(a["net_flow"], 1.0 - 1.5 - 0.4)
        self.assertIsNone(t[TOK_V]["change_pct"])        # single trade: no change claimed
        self.assertEqual(web.trending(db, 24, sort="change"), [])   # nothing has 5+ trades
        self.assertTrue(web.trending(db, 24, new_only=True))
        self.assertEqual(web.trending(db, 24, sort="nonsense")[0]["token"], web.trending(db, 24)[0]["token"])
        db.close()

    def test_gainers_and_buy_pressure_rank_correctly(self):
        path = os.path.join(self.dir, "g.db")
        db = DB(path)
        now = int(time.time())
        up, dn = "0x" + "d1" * 20, "0x" + "d2" * 20
        rows = []
        for i in range(6):                       # price rises 1.0 -> 2.0 per token, all buys
            rows.append(trade(100 + i, "buy", ALICE, up, 10, 10 * (1 + i / 5), 10 + i))
        for i in range(6):                       # price falls 1.0 -> 0.5, mostly sells
            rows.append(trade(200 + i, "sell" if i else "buy", BOB, dn, 10, 10 * (1 - i / 10), 20 + i))
        db.save([launch(up, "UP", cfg.ZERO, now, 1), launch(dn, "DN", cfg.ZERO, now, 2)], rows,
                {b: now - 60 for b in range(1, 40)})
        rd = web.ReadDB(path)
        g = web.trending(rd, 24, sort="change")
        self.assertEqual([r["symbol"] for r in g], ["UP", "DN"])
        self.assertAlmostEqual(g[0]["change_pct"], 100.0)
        self.assertAlmostEqual(g[1]["change_pct"], -50.0)
        b = web.trending(rd, 24, sort="buys")
        self.assertEqual([r["symbol"] for r in b], ["UP", "DN"])
        self.assertAlmostEqual(b[0]["buy_share"], 1.0)
        rd.close()

    def test_creator_quality(self):
        path = os.path.join(self.dir, "c.db")
        db = DB(path)
        now = int(time.time())
        c1, c2 = "0x" + "e1" * 20, "0x" + "e2" * 20
        toks = [("0x" + "f%d" % i + "0" * 38, c1 if i < 4 else c2) for i in range(5)]
        launches = []
        for t, c in toks:
            l = launch(t, "T", cfg.ZERO, now, 1)
            l["creator"] = c
            launches.append(l)
        rows, n = [], 1
        for i, (t, c) in enumerate(toks):
            if i == 3:
                continue                                  # a launched token nobody traded
            rising = i in (0, 1, 4)                       # 0,1 rise (c1); 2 falls (c1); 4 rises (c2)
            for j in range(2):
                price = (1.0 + j) if rising else (2.0 - j)
                rows.append(trade(n, "buy", ALICE, t, 10, 10 * price, 10 + n))
                n += 1
        db.save(launches, rows, {b: now - 60 for b in range(1, 40)})
        rd = web.ReadDB(path)
        by = {r["creator"]: r for r in web.creators(rd, 10)}
        a = by[c1]
        self.assertEqual((a["tokens_launched"], a["traded"], a["up"], a["active"]), (4, 3, 2, 3))
        self.assertAlmostEqual(a["hit_rate"], 2 / 3)
        self.assertEqual((by[c2]["tokens_launched"], by[c2]["traded"], by[c2]["up"]), (1, 1, 1))
        self.assertEqual([r["creator"] for r in web.creators(rd, 10, sort="hit")], [c1])   # c2 has <3 traded
        self.assertEqual(web.creators(rd, 10, sort="nonsense")[0]["creator"], web.creators(rd, 10)[0]["creator"])
        rd.close()

    def test_trending_endpoint_params(self):
        for q in ("sort=change", "sort=buys&new=1", "sort=%27%3B--", "hours=banana&sort=traders"):
            s, r = self.j("/api/trending?" + q)
            self.assertEqual(s, 200, q)

    def test_search(self):
        s, r = self.j("/api/search?q=aaa")                      # symbol, case-insensitive
        self.assertEqual([t["token"] for t in r["tokens"]], [TOK_A])
        s, r = self.j("/api/search?q=aaa%20coin")               # name
        self.assertEqual(len(r["tokens"]), 1)
        s, r = self.j(f"/api/search?q={ALICE}")                 # wallet address
        self.assertEqual((r["wallets"], r["tokens"]), ([{"wallet": ALICE}], []))
        s, r = self.j(f"/api/search?q={TOK_A.upper().replace('0X', '0x')}")   # token address, any case
        self.assertEqual(len(r["tokens"]), 1)
        s, r = self.j("/api/search?q=" + "0x" + "7" * 40)       # valid but unknown address
        self.assertEqual((r["wallets"], r["tokens"]), ([], []))
        for junk in ("a", "", "%25%25", "_", "x%27%20OR%201=1--"):    # too short, wildcards, injection
            s, r = self.j("/api/search?q=" + junk)
            self.assertEqual(s, 200)
            self.assertEqual((r["tokens"], r["wallets"]), ([], []), junk)

    def test_wallet_win_rate_and_stats(self):
        web._cache.clear()
        s, w = self.j("/api/wallets?quote=eth")
        alice = next(r for r in w if r["trader"] == ALICE)
        self.assertEqual((alice["positions"], alice["wins"]), (1, 1))
        self.assertAlmostEqual(alice["win_rate"], 1.0)
        s, d = self.j(f"/api/wallet/{ALICE}")
        st = d["stats"][0]
        self.assertEqual((st["quote"], st["positions"], st["wins"]), ("ETH", 1, 1))
        self.assertAlmostEqual(st["pnl"], 0.5)
        self.assertEqual(st["best"]["token"], TOK_A)
        self.assertEqual(d["excluded"], 0)
        s, d = self.j(f"/api/wallet/{BOB}")               # bob: two positions cannot be scored
        self.assertEqual(d["excluded"], 2)  # TOK_A sold without a seen buy; TOK_X has no known quote token
        s, d = self.j(f"/api/wallet/{CREATOR}")           # creator who never traded still has a page
        self.assertEqual((s, d["stats"], len(d["created"])), (200, [], 2))

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
