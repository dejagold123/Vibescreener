"""Wallet sign-in, token credits, the deposit watcher, and the credit-aware endpoints."""
import json
import os
import secrets
import tempfile
import unittest
from unittest import mock

from eth_account import Account
from eth_account.messages import encode_defunct

from test_ask import GOOD, IP
from test_brief import Resp, Session
from vibescreener import credits, ethsig, web
from vibescreener.db import DB

TOKEN = "0x" + "ab" * 20
TREASURY = "0x" + "cd" * 20
ST = {"enabled": True, "token": TOKEN, "treasury": TREASURY, "price": "1", "price_micro": 1_000_000}
ENVV = {"TOKEN_ADDRESS": TOKEN, "TREASURY_ADDRESS": TREASURY, "ASK_PRICE": "1"}


def sign(acct, message):
    return acct.sign_message(encode_defunct(text=message)).signature.hex()


def tmpdb():
    path = os.path.join(tempfile.mkdtemp(), "c.db")
    DB(path)
    return path


class SigTests(unittest.TestCase):
    def test_keccak_vectors(self):
        self.assertEqual(ethsig.keccak256(b"").hex(),
                         "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertEqual(ethsig.keccak256(b"abc").hex(),
                         "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")

    def test_recovery_matches_eth_account(self):
        for _ in range(5):
            a = Account.create()
            msg = "hello " + secrets.token_hex(8)
            self.assertEqual(ethsig.recover_personal(msg, sign(a, msg)), a.address.lower())

    def test_garbage_signatures_do_not_raise(self):
        for bad in ("", "0x", "0x12", "zz" * 65, None):
            self.assertIsNone(ethsig.recover_personal("m", bad))


class SignInTests(unittest.TestCase):
    def setUp(self):
        self.path = tmpdb()
        self.db = web.WriteDB(self.path)
        self.acct = Account.create()
        self.w = self.acct.address.lower()

    def test_full_flow_and_session(self):
        msg = credits.issue_nonce(self.db, self.acct.address, now=1000)
        tok = credits.login(self.db, self.acct.address, sign(self.acct, msg), now=1001)
        self.assertEqual(credits.wallet_for(self.db, tok, now=1002), self.w)
        self.assertIsNone(credits.wallet_for(self.db, tok, now=1001 + credits.SESSION_TTL + 5))
        self.assertIsNone(credits.wallet_for(self.db, "x" * 64))
        self.assertIsNone(credits.wallet_for(self.db, ""))

    def test_nonce_is_single_use(self):
        msg = credits.issue_nonce(self.db, self.w, now=1000)
        sig = sign(self.acct, msg)
        credits.login(self.db, self.w, sig, now=1001)
        with self.assertRaises(credits.CreditsError) as cm:
            credits.login(self.db, self.w, sig, now=1002)
        self.assertEqual(cm.exception.status, 401)

    def test_expired_nonce_and_wrong_signer(self):
        msg = credits.issue_nonce(self.db, self.w, now=1000)
        with self.assertRaises(credits.CreditsError):
            credits.login(self.db, self.w, sign(self.acct, msg), now=1000 + credits.NONCE_TTL + 1)
        msg = credits.issue_nonce(self.db, self.w, now=1000)
        with self.assertRaises(credits.CreditsError):
            credits.login(self.db, self.w, sign(Account.create(), msg), now=1001)
        # a failed attempt burns the nonce
        with self.assertRaises(credits.CreditsError):
            credits.login(self.db, self.w, sign(self.acct, msg), now=1002)

    def test_signature_for_other_message_is_rejected(self):
        credits.issue_nonce(self.db, self.w, now=1000)
        with self.assertRaises(credits.CreditsError):
            credits.login(self.db, self.w, sign(self.acct, "something else"), now=1001)

    def test_bad_wallet(self):
        for bad in ("", "0x12", "nope"):
            with self.assertRaises(credits.CreditsError):
                credits.issue_nonce(self.db, bad)


class BalanceTests(unittest.TestCase):
    def setUp(self):
        self.db = web.WriteDB(tmpdb())

    def test_take_refund_and_no_overspend(self):
        w = "0x" + "11" * 20
        self.assertFalse(credits.take(self.db, w, 1))
        self.assertTrue(credits.credit_deposit(self.db, "0xt1", 0, w, 2_500_000, 1))
        self.assertTrue(credits.take(self.db, w, 1_000_000))
        self.assertTrue(credits.take(self.db, w, 1_000_000))
        self.assertFalse(credits.take(self.db, w, 1_000_000))
        self.assertEqual(credits.balance(self.db, w), 500_000)
        credits.refund(self.db, w, 1_000_000)
        self.assertEqual(credits.tokens_text(credits.balance(self.db, w)), "1.5")

    def test_deposit_is_idempotent(self):
        w = "0x" + "22" * 20
        self.assertTrue(credits.credit_deposit(self.db, "0xt", 3, w, 5, 1))
        self.assertFalse(credits.credit_deposit(self.db, "0xt", 3, w, 5, 1))
        self.assertTrue(credits.credit_deposit(self.db, "0xt", 4, w, 5, 1))      # other log in same tx
        self.assertEqual(credits.balance(self.db, w), 10)

    def test_tokens_text(self):
        self.assertEqual([credits.tokens_text(x) for x in (0, 1, 1_000_000, 1_234_500)],
                         ["0", "0.000001", "1", "1.2345"])


class FakeExplorer:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, path, params=None):
        self.calls.append((path, params))
        return self.pages[len(self.calls) - 1] if len(self.calls) <= len(self.pages) else {"items": []}


def item(tx, idx, frm, to, value, block, decimals="18"):
    return {"from": {"hash": frm}, "to": {"hash": to}, "total": {"value": value, "decimals": decimals},
            "transaction_hash": tx, "log_index": idx, "block_number": block,
            "timestamp": "2026-10-01T10:00:00.000000Z"}


class PollTests(unittest.TestCase):
    def setUp(self):
        self.path = tmpdb()
        self.db = DB(self.path)
        self.reader = web.WriteDB(self.path)
        self.a, self.b = "0x" + "aa" * 20, "0x" + "bb" * 20

    def test_credits_only_real_deposits_and_paginates(self):
        ex = FakeExplorer([
            {"items": [item("0x1", 0, self.a, TREASURY, str(3 * 10 ** 18), 50),
                       item("0x2", 0, self.a, "0x" + "ee" * 20, str(10 ** 18), 50),        # someone else
                       item("0x3", 0, "0x" + "0" * 40, TREASURY, str(10 ** 18), 49),        # mint
                       item("0x4", 0, TREASURY, TREASURY, str(10 ** 18), 49)],             # self
             "next_page_params": {"block_number": 49}},
            {"items": [item("0x5", 1, self.b, TREASURY.upper().replace("0X", "0x"), str(5 * 10 ** 17), 40)]},
        ])
        new = credits.poll(self.db, ex, ST, log=lambda *_: None)
        self.assertEqual(sorted(new), sorted([(self.a, 3_000_000), (self.b, 500_000)]))
        self.assertEqual(len(ex.calls), 2)
        self.assertEqual(credits.balance(self.reader, self.a), 3_000_000)
        self.assertEqual(self.db.get_state("credit_scan_block"), "50")

    def test_rescan_does_not_double_credit_and_stops_at_cursor(self):
        page = {"items": [item("0x1", 0, self.a, TREASURY, str(10 ** 18), 500)], "next_page_params": {"x": 1}}
        credits.poll(self.db, FakeExplorer([page]), ST, log=lambda *_: None)
        old = {"items": [item("0x0", 0, self.a, TREASURY, str(10 ** 18), 5)]}
        ex = FakeExplorer([page, old])
        self.assertEqual(credits.poll(self.db, ex, ST, log=lambda *_: None), [])
        self.assertEqual(credits.balance(self.reader, self.a), 1_000_000)
        self.assertEqual(len(ex.calls), 2)       # page 2 started but its old item ended the scan

    def test_decimals_and_junk(self):
        ex = FakeExplorer([{"items": [
            item("0x1", 0, self.a, TREASURY, "2500000", 10, decimals="6"),
            item("0x2", 0, self.a, TREASURY, "oops", 10),
            item("0x3", 0, self.a, TREASURY, "0", 10),
            {"to": None, "from": None}]}])
        credits.poll(self.db, ex, ST, log=lambda *_: None)
        self.assertEqual(credits.balance(self.reader, self.a), 2_500_000)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.path = tmpdb()
        self.acct = Account.create()
        self.w = self.acct.address.lower()
        p = mock.patch.dict(os.environ, ENVV)
        p.start()
        self.addCleanup(p.stop)
        self.body = json.dumps({"question": "How is the market?"}).encode()

    def login(self):
        st, out = web.route(self.path, "/api/auth/nonce", {"wallet": [self.w], "_ip": ["9.9.9.9"]})
        self.assertEqual(st, 200)
        st, out = web.post_login(self.path, json.dumps(
            {"wallet": self.w, "signature": sign(self.acct, out["message"])}).encode(), "9.9.9.9")
        self.assertEqual(st, 200, out)
        return out["session"]

    def test_config_hides_nothing_secret_and_respects_switch(self):
        st, out = web.route(self.path, "/api/credits/config", {})
        self.assertEqual((st, out["enabled"], out["treasury"]), (200, True, TREASURY))
        with mock.patch.dict(os.environ, {"TOKEN_ADDRESS": ""}):
            self.assertFalse(web.route(self.path, "/api/credits/config", {})[1]["enabled"])

    def test_login_balance_and_bad_inputs(self):
        self.assertEqual(web.route(self.path, "/api/credits/balance", {"_auth": [""]})[0], 401)
        tok = self.login()
        db = web.WriteDB(self.path)
        credits.credit_deposit(db, "0xt", 0, self.w, 4_000_000, 1)
        st, out = web.route(self.path, "/api/credits/balance", {"_auth": [tok]})
        self.assertEqual((st, out["balance"], out["wallet"]), (200, "4", self.w))
        self.assertEqual([(d["tx"], d["amount"]) for d in out["deposits"]], [("0xt", "4")])
        self.assertEqual(web.post_login(self.path, b"nope", "1.1.1.1")[0], 400)
        self.assertEqual(web.post_login(self.path, json.dumps({"wallet": self.w, "signature": "0x00"}).encode(),
                                        "1.1.1.1")[0], 401)

    def test_paying_visitor_spends_after_free_runs_out(self):
        tok = self.login()
        db = web.WriteDB(self.path)
        credits.credit_deposit(db, "0xt", 0, self.w, 1_000_000, 1)
        env = {"ASK_FREE_PER_DAY": "1", "ASK_GLOBAL_PER_DAY": "50"}
        with mock.patch.dict(os.environ, env), \
                mock.patch("vibescreener.llm.enabled", return_value=False):
            first = web.post_ask(self.path, self.body, IP, tok)
            second = web.post_ask(self.path, self.body, IP, tok)
            third = web.post_ask(self.path, self.body, IP, tok)
        self.assertEqual(first[0], 200)
        self.assertEqual(first[1]["credits"]["balance"], "1")           # free question, nothing spent
        self.assertEqual(second[0], 200)
        self.assertEqual(second[1]["credits"]["balance"], "1")          # no AI -> refunded, still 1
        self.assertEqual(third[0], 200)

    def test_without_login_limit_is_429_and_mentions_credits(self):
        with mock.patch.dict(os.environ, {"ASK_FREE_PER_DAY": "0", "ASK_GLOBAL_PER_DAY": "50"}):
            st, out = web.post_ask(self.path, self.body, IP)
        self.assertEqual(st, 429)
        self.assertIn("credits", out["error"].lower())

    def test_forged_bearer_gets_no_credits(self):
        with mock.patch.dict(os.environ, {"ASK_FREE_PER_DAY": "0", "ASK_GLOBAL_PER_DAY": "50"}):
            st, out = web.post_ask(self.path, self.body, IP, "f" * 64)
        self.assertEqual(st, 429)


if __name__ == "__main__":
    unittest.main()
