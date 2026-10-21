"""Token table, price-change windows and candles, on a database with controlled timestamps."""
import os
import tempfile
import time
import unittest

from test_web import W, launch, trade
from vibescreener import config as cfg
from vibescreener import web
from vibescreener.db import DB

NOW = int(time.time())
A, V, N, G = ("0x" + c * 20 for c in ("a1", "b2", "c3", "d4"))     # eth, vibevibe, new-no-trades, gainer
WALLETS = ["0x" + f"{i:02x}" * 20 for i in range(1, 8)]


def seed(path):
    db = DB(path)
    ts, trades, n = {}, [], [0]

    def add(side, w, tok, tokens, quote, age):
        n[0] += 1
        b = 100 + n[0]
        ts[b] = NOW - age
        trades.append(trade(n[0], side, w, tok, tokens, quote, b))

    # A: price 1.0 an hour and a half ago, then 2.0 now (same token amount, double the quote)
    add("buy", WALLETS[0], A, 10, 10, 5400)
    add("buy", WALLETS[1], A, 10, 20, 30)
    add("sell", WALLETS[1], A, 10, 15, 20)
    add("buy", WALLETS[2], V, 10, 5, 7200)
    # G: five buys by three wallets inside the last 10 minutes, price rising
    for i, q in enumerate((1, 1.2, 1.4, 1.6, 2.0)):
        add("buy", WALLETS[i % 3], G, 10, q, 600 - i * 60)
    launches = [launch(A, "AAA", cfg.ZERO, NOW, 1), launch(V, "VVV", cfg.VIBEVIBE_TOKEN, NOW, 2),
                launch(G, "GGG", cfg.ZERO, NOW, 3), launch(N, "NEW", cfg.ZERO, NOW, 4)]
    db.save(launches, trades, ts)
    db.conn.execute("UPDATE tokens SET ts = ?", (NOW - 100,))
    db.conn.commit()
    return db


class PairsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.path = os.path.join(cls.dir, "p.db")
        seed(cls.path)

    def rd(self):
        return web.ReadDB(self.path)

    def test_changes_use_the_last_price_before_the_window(self):
        now = 10_000
        ch = web._changes([(now - 7200, 1.0), (now - 1800, 1.5), (now - 10, 2.0)], now)
        self.assertAlmostEqual(ch["5m"], 33.333, places=2)      # 1.5 -> 2.0
        self.assertAlmostEqual(ch["1h"], 100.0)                 # 1.0 -> 2.0 (opened before the hour)
        self.assertIsNone(web._changes([], now)["1h"])
        self.assertIsNone(web._changes([(now - 10, 2.0)], now)["5m"])   # one price, nothing to compare

    def test_buys_only_series_ignores_the_fee_gap_between_sides(self):
        t = [(NOW - 40, "buy", "w1", 1, 1.00), (NOW - 30, "buy", "w2", 1, 1.00), (NOW - 20, "sell", "w3", 1, 0.90)]
        self.assertAlmostEqual(web._windows(t, NOW)["ch"]["1h"], 0.0)

    def test_quote_filter_and_window_counts(self):
        db = self.rd()
        eth = web.pairs(db, "eth", "trending", "24h")["rows"]
        self.assertEqual({r["token"] for r in eth}, {A, G})
        self.assertEqual({r["token"] for r in web.pairs(db, "vibevibe", "trending", "24h")["rows"]}, {V})
        self.assertEqual({r["token"] for r in web.pairs(db, "all", "trending", "24h")["rows"]}, {A, V, G})
        a = next(r for r in eth if r["token"] == A)
        self.assertEqual((a["txns"], a["buys"], a["sells"], a["makers"]), (3, 2, 1, 2))
        self.assertAlmostEqual(a["volume"], 45.0)
        # 5m window: only A's last two trades (30s and 20s ago); G's newest is 120s old but A's change is vs 10 -> 20
        r5 = {r["token"]: r for r in web.pairs(db, "eth", "trending", "5m")["rows"]}
        self.assertEqual(r5[A]["txns"], 2)
        self.assertNotIn(V, r5)
        db.close()

    def test_changes_reach_the_rows(self):
        db = self.rd()
        a = next(r for r in web.pairs(db, "eth", "trending", "24h")["rows"] if r["token"] == A)
        self.assertAlmostEqual(a["ch1h"], 100.0)               # 1.0 an hour and a half ago -> 2.0 now
        self.assertAlmostEqual(a["price"], 1.5)                # last trade was the sell at 1.5
        db.close()

    def test_new_tab_lists_launches_without_trades(self):
        db = self.rd()
        toks = [r["token"] for r in web.pairs(db, "eth", "new", "24h")["rows"]]
        self.assertIn(N, toks)
        self.assertNotIn(N, [r["token"] for r in web.pairs(db, "eth", "trending", "24h")["rows"]])
        db.close()

    def test_gainers_need_five_trades_and_three_makers(self):
        db = self.rd()
        g = web.pairs(db, "eth", "gainers", "1h")["rows"]
        self.assertEqual([r["token"] for r in g], [G])          # A has only 3 txns in 24h, 2 makers
        self.assertGreater(g[0]["change"], 0)
        db.close()

    def test_volume_ranking_only_inside_one_quote_token(self):
        db = self.rd()
        self.assertTrue(web.pairs(db, "eth", "volume", "24h")["volume_comparable"])
        out = web.pairs(db, "all", "volume", "24h")
        self.assertFalse(out["volume_comparable"])
        self.assertEqual(web.pairs(db, "nonsense", "bogus", "99h")["window"], "24h")
        db.close()

    def test_candles_ohlc_and_gap_filling(self):
        db = self.rd()
        c = web.candles(db, A, 60)["candles"]
        self.assertEqual(sum(x["buys"] + x["sells"] for x in c), 3)
        first, last = c[0], c[-1]
        self.assertEqual((first["o"], first["c"]), (1.0, 1.0))       # 10 tokens for 10
        self.assertEqual(last["c"], 1.5)
        self.assertGreater(len(c), 50)                               # ~90 minutes of empty minutes filled
        gap = c[len(c) // 2]
        self.assertEqual((gap["v"], gap["o"], gap["c"]), (0, 1.0, 1.0))   # flat at the previous close
        self.assertEqual(web.candles(db, "0x" + "9" * 40, 60)["candles"], [])
        self.assertEqual(web.candles(db, A, 7)["interval"], 300)     # unknown interval -> default
        db.close()

    def test_token_detail_has_window_stats(self):
        db = self.rd()
        st = web.token_detail(db, A)["stats"]
        self.assertEqual(st["w"]["24h"]["txns"], 3)
        self.assertAlmostEqual(st["ch"]["1h"], 100.0)
        db.close()

    def test_endpoints(self):
        s, p = web.route(self.path, "/api/pairs", {"quote": ["eth"]})
        self.assertEqual(s, 200)
        self.assertEqual(web.route(self.path, f"/api/token/{A}/candles", {"interval": ["60"]})[0], 200)
        self.assertEqual(web.route(self.path, "/api/token/xyz/candles", {})[0], 400)


if __name__ == "__main__":
    unittest.main()
