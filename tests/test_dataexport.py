"""Paid CSV exports: keys, plan gating, spreadsheet-safe cells, limits."""
import csv
import hashlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from test_pairs import A, seed
from vibescreener import credits, dataexport, web

ST = {"enabled": True, "token": "0x" + "ab" * 20, "treasury": "0x" + "cd" * 20, "symbol": "VSCR",
      "api_price": "10", "api_micro": 10_000_000, "plan_days": 30}
ME = "0x" + "77" * 20


class CellTests(unittest.TestCase):
    def test_formula_and_control_characters_are_defused(self):
        for bad in ("=1+1", "+cmd", "-2+3", "@SUM(A1)", "\tx", "\x01x"):
            self.assertTrue(dataexport.cell(bad).startswith("'"), bad)
        self.assertEqual(dataexport.cell("plain"), "plain")
        self.assertEqual((dataexport.cell(None), dataexport.cell(5), dataexport.cell(True)), ("", "5", "true"))
        self.assertNotIn("\n", dataexport.cell("a\nb"))
        self.assertEqual(dataexport.cell(-3), "-3")                      # real negative numbers stay numbers

    def test_to_csv_roundtrips_commas_and_quotes(self):
        text = dataexport.to_csv(["a", "b"], [{"a": 'he said "hi", ok', "b": "=HYPERLINK(1)"}])
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[1], ['he said "hi", ok', "'=HYPERLINK(1)"])


class Base(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "x.db")
        seed(self.path)
        self.w = web.WriteDB(self.path)
        credits.credit_deposit(self.w, "0xt", 0, ME, 30_000_000, 1)
        p = mock.patch.dict(os.environ, {"TOKEN_ADDRESS": ST["token"], "TREASURY_ADDRESS": ST["treasury"]})
        p.start()
        self.addCleanup(p.stop)
        web._hits.clear()

    def tearDown(self):
        self.w.close()

    def key(self):
        dataexport.handle(self.w, ME, "buy", ST)
        return dataexport.handle(self.w, ME, "new", ST)["key"]


class KeyTests(Base):
    def test_key_needs_a_plan_and_is_stored_hashed(self):
        with self.assertRaises(dataexport.ExportError) as cm:
            dataexport.handle(self.w, ME, "new", ST)
        self.assertEqual(cm.exception.status, 402)
        key = self.key()
        self.assertRegex(key, r"^vsk_[0-9a-f]{40}$")
        raw = json.dumps(self.w.q("SELECT * FROM api_keys"))
        self.assertNotIn(key, raw)
        self.assertIn(hashlib.sha256(key.encode()).hexdigest(), raw)
        self.assertEqual(dataexport.info(self.w, ME)["key_prefix"], key[:8])

    def test_new_key_replaces_old_one(self):
        old = self.key()
        new = dataexport.handle(self.w, ME, "new", ST)["key"]
        self.assertIsNone(dataexport.wallet_for_key(self.w, old))
        self.assertEqual(dataexport.wallet_for_key(self.w, new), ME)

    def test_key_dies_with_the_plan(self):
        key = self.key()
        self.assertEqual(dataexport.wallet_for_key(self.w, key), ME)
        later = dataexport.credits.plan_expires(self.w, ME, "api") + 5
        self.assertIsNone(dataexport.wallet_for_key(self.w, key, now=later))
        for bad in (None, "", "vsk_short", "x" * 44, key.upper()):
            self.assertIsNone(dataexport.wallet_for_key(self.w, bad))

    def test_buying_again_extends_the_plan(self):
        dataexport.handle(self.w, ME, "buy", ST)
        e1 = credits.plan_expires(self.w, ME, "api")
        dataexport.handle(self.w, ME, "buy", ST)
        self.assertEqual(credits.plan_expires(self.w, ME, "api"), e1 + 30 * 86400)
        dataexport.handle(self.w, ME, "buy", ST)                         # third and last affordable purchase
        with self.assertRaises(dataexport.ExportError):
            dataexport.handle(self.w, ME, "buy", ST)                     # 30 spent, nothing left


class ExportTests(Base):
    def get(self, name, key, query=None):
        return dataexport.respond(self.path, name, query or {}, key)

    def test_requires_a_valid_key(self):
        for k in (None, "", "vsk_" + "0" * 40):
            self.assertEqual(self.get("tokens", k)[0], 401)

    def test_every_dataset_returns_csv_with_header(self):
        key = self.key()
        for name in ("tokens", "trades", "wallets", "creators"):
            st, body, ctype = self.get(name, key)
            self.assertEqual((st, ctype), (200, "text/csv; charset=utf-8"), name)
            rows = list(csv.reader(io.StringIO(body.decode())))
            self.assertGreaterEqual(len(rows), 2, name)
        self.assertEqual(self.get("nonsense", key)[0], 404)

    def test_trades_filter_and_limit(self):
        key = self.key()
        _, body, _ = self.get("trades", key, {"token": [A]})
        rows = list(csv.DictReader(io.StringIO(body.decode())))
        self.assertTrue(rows and all(r["token"] == A for r in rows))
        _, body, _ = self.get("trades", key, {"limit": ["1"]})
        self.assertEqual(len(list(csv.DictReader(io.StringIO(body.decode())))), 1)
        self.assertEqual(self.get("trades", key, {"token": ["bad"]})[0], 400)

    def test_hostile_token_name_is_safe_in_the_csv(self):
        key = self.key()
        self.w.conn.execute("UPDATE tokens SET name = ?, symbol = ? WHERE token = ?", ("=cmd|' /C calc'!A0", "@evil", A))
        self.w.conn.commit()
        _, body, _ = self.get("tokens", key)
        rows = {r["token"]: r for r in csv.DictReader(io.StringIO(body.decode()))}
        self.assertTrue(rows[A]["name"].startswith("'="))
        self.assertTrue(rows[A]["symbol"].startswith("'@"))

    def test_per_key_rate_limit(self):
        key = self.key()
        codes = [self.get("creators", key)[0] for _ in range(22)]
        self.assertEqual(codes[:20], [200] * 20)
        self.assertEqual(codes[20], 429)

    def test_post_endpoints_need_sign_in(self):
        self.assertEqual(web.post_apikey(self.path, "me", b"{}", "7.7.7.1")[0], 401)
        tok = "c" * 64
        self.w.conn.execute("INSERT INTO sessions VALUES (?,?,?)",
                            (hashlib.sha256(tok.encode()).hexdigest(), ME, 9_999_999_999))
        self.w.conn.commit()
        st, out = web.post_apikey(self.path, "buy", b"{}", "7.7.7.2", tok)
        self.assertEqual((st, out["balance"]), (200, "20"))
        st, out = web.post_apikey(self.path, "new", b"{}", "7.7.7.3", tok)
        self.assertEqual(st, 200)
        self.assertTrue(out["key"].startswith("vsk_"))
        self.assertEqual(web.post_apikey(self.path, "zzz", b"{}", "7.7.7.4", tok)[0], 404)
        st, out = web.route(self.path, "/api/apikey/me", {"_auth": [tok]})
        self.assertEqual((st, bool(out["plan_expires"])), (200, True))


if __name__ == "__main__":
    unittest.main()
