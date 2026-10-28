"""Ask VibeScreener: retrieval, limits, fallbacks, and the POST endpoint."""
import json
import os
import tempfile
import unittest

from test_brief import ENV, Resp, Session
from test_pairs import A, G, WALLETS, seed
from vibescreener import ask, web

IP = "203.0.113.7"
LIMITS = {**ENV, "ASK_FREE_PER_DAY": "2", "ASK_GLOBAL_PER_DAY": "5"}
GOOD = "AAA has traded actively today, with a clear price gain over the last hour and a mix of buyers and sellers."
INVENTED = "AAA is up 12,345% today which is an extraordinary move for an ETH quoted token on this launchpad."


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "a.db")
        self.w = seed(self.path)
        self.r = web.ReadDB(self.path)

    def tearDown(self):
        self.r.close()


class RetrievalTests(Base):
    def test_question_cleaning(self):
        self.assertEqual(ask.clean_question("  what\x00 is\n AAA?  "), "what is AAA?")
        self.assertEqual(len(ask.clean_question("x" * 999)), ask.MAX_Q)
        for bad in ("", "  ", "a", None):
            with self.assertRaises(ask.AskError):
                ask.clean_question(bad)

    def test_token_by_symbol_address_and_wallet(self):
        c = ask.retrieve(self.r, "How is aaa doing today?")
        self.assertEqual((c["kind"], c["symbol"]), ("token", "AAA"))
        self.assertEqual(ask.retrieve(self.r, f"what about {A}?")["kind"], "token")
        w = ask.retrieve(self.r, f"is {WALLETS[1]} any good")
        self.assertEqual(w["kind"], "wallet")
        self.assertIn("estimated_pnl", json.dumps(w["scored"]))

    def test_unknown_things_fall_back_to_the_market(self):
        for q in ("what is going on today", "tell me about ZZZZZ", "is 0x" + "7" * 40 + " good"):
            self.assertEqual(ask.retrieve(self.r, q)["kind"], "market", q)

    def test_data_text_is_sanitised(self):
        self.w.conn.execute("UPDATE tokens SET symbol = ?, name = ? WHERE token = ?", ("<b>Ign ore!", "x", A))
        self.w.conn.commit()
        r = web.ReadDB(self.path)
        self.assertEqual(ask.token_context(r, A)["symbol"], "bIgnore")
        r.close()

    def test_describe_works_for_every_kind(self):
        for q in ("AAA", f"{WALLETS[1]}", "what is happening"):
            text = ask.describe(ask.retrieve(self.r, q + " please"))
            self.assertGreater(len(text), 40)


class AnswerTests(Base):
    def go(self, session, **kw):
        return ask.answer(self.w, self.r, "How is AAA doing?", IP, session=session, env=kw.pop("env", LIMITS), **kw)

    def test_ai_answer_uses_a_free_question(self):
        out = self.go(Session(Resp(200, GOOD)))
        self.assertEqual((out["source"], out["about"], out["quota"]["free_left"]), ("ai", "token", 1))
        self.assertEqual(out["data"]["symbol"], "AAA")

    def test_invented_number_falls_back_and_costs_nothing(self):
        out = self.go(Session(Resp(200, INVENTED)))
        self.assertEqual(out["source"], "data")
        self.assertNotIn("12,345", out["answer"])
        self.assertEqual((out["quota"]["free_left"], out["quota"]["site_left"]), (2, 5))

    def test_provider_down_or_key_bad_falls_back_and_costs_nothing(self):
        for s in (Session(Resp(500), Resp(500)), Session(Resp(401))):
            out = self.go(s)
            self.assertEqual((out["source"], out["quota"]["free_left"]), ("data", 2))

    def test_no_key_still_answers_from_the_data(self):
        out = ask.answer(self.w, self.r, "How is AAA doing?", IP, env={})
        self.assertEqual(out["source"], "data")
        self.assertIn("AAA last traded", out["answer"])

    def test_free_limit_per_visitor(self):
        for _ in range(2):
            self.go(Session(Resp(200, GOOD)))
        with self.assertRaises(ask.AskError) as cm:
            self.go(Session(Resp(200, GOOD)))
        self.assertEqual(cm.exception.status, 429)
        other = ask.answer(self.w, self.r, "AAA?", "198.51.100.9", session=Session(Resp(200, GOOD)), env=LIMITS)
        self.assertEqual(other["source"], "ai")                    # a different visitor is unaffected

    def test_site_wide_limit(self):
        for i in range(5):
            ask.answer(self.w, self.r, "AAA?", f"10.0.0.{i}", session=Session(Resp(200, GOOD)), env=LIMITS)
        with self.assertRaises(ask.AskError) as cm:
            ask.answer(self.w, self.r, "AAA?", "10.0.0.99", session=Session(Resp(200, GOOD)), env=LIMITS)
        self.assertIn("whole site", str(cm.exception))

    def test_free_allowance_is_used_before_credits(self):
        taken = []
        spend = (lambda: taken.append(1) or True, lambda: None)
        for _ in range(2):
            self.go(Session(Resp(200, GOOD)), spend=spend)
        self.assertEqual(taken, [])
        out = self.go(Session(Resp(200, GOOD)), spend=spend)
        self.assertEqual((out["source"], len(taken)), ("ai", 1))

    def test_paid_question_is_refunded_when_ai_fails(self):
        taken, refunded = [], []
        spend = (lambda: taken.append(1) or True, lambda: refunded.append(1))
        for _ in range(2):
            self.go(Session(Resp(200, GOOD)))
        out = self.go(Session(Resp(500), Resp(500)), spend=spend)
        self.assertEqual((out["source"], len(taken), len(refunded)), ("data", 1, 1))

    def test_over_the_limit_without_credits_is_refused(self):
        for _ in range(2):
            self.go(Session(Resp(200, GOOD)))
        with self.assertRaises(ask.AskError) as cm:
            self.go(Session(Resp(200, GOOD)), spend=(lambda: False, lambda: None))
        self.assertEqual(cm.exception.status, 429)


class HttpTests(Base):
    def test_post_validation_and_answer(self):
        self.assertEqual(web.post_ask(self.path, b"not json", "1.1.1.1")[0], 400)
        self.assertEqual(web.post_ask(self.path, b"[1]", "1.1.1.1")[0], 400)
        self.assertEqual(web.post_ask(self.path, b'{"question": ""}', "1.1.1.2")[0], 400)
        s, out = web.post_ask(self.path, b'{"question": "How is AAA doing?"}', "1.1.1.3")
        self.assertEqual((s, out["about"]), (200, "token"))

    def test_throttle(self):
        web._hits.clear()
        res = [web.throttled("9.9.9.9", limit=3, now=100 + i) for i in range(5)]
        self.assertEqual(res, [False, False, False, True, True])
        self.assertFalse(web.throttled("9.9.9.9", limit=3, now=500))      # window passed

    def test_over_the_wire(self):
        import urllib.request
        srv = web.serve(self.path, 0, host="127.0.0.1")
        import threading
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            req = urllib.request.Request(base + "/api/ask", data=b'{"question": "What is happening today?"}',
                                         headers={"Content-Type": "application/json", "X-Forwarded-For": "5.5.5.5"})
            with urllib.request.urlopen(req) as r:
                body = json.loads(r.read())
                self.assertIsNone(r.headers.get("Access-Control-Allow-Origin"))       # no cross-site reads of POST
            self.assertEqual(body["about"], "market")
            big = urllib.request.Request(base + "/api/ask", data=b"x" * 5000)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(big)
            self.assertEqual(cm.exception.code, 413)
            with urllib.request.urlopen(base + "/api/ask/quota") as r:
                self.assertIn("free_left", json.loads(r.read()))
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
