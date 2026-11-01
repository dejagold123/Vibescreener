"""Watchlists, Telegram linking and alert sending (no network: Telegram and the sender are fakes)."""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from test_pairs import A, NOW, WALLETS, seed
from vibescreener import alerts, credits, web

ST = {"enabled": True, "token": "0x" + "ab" * 20, "treasury": "0x" + "cd" * 20, "symbol": "VSCR", "telegram": True,
      "alerts_price": "5", "alerts_micro": 5_000_000, "plan_days": 30}
ME = "0x" + "77" * 20


class FakeTg:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, chat, text):
        if self.fail:
            raise alerts.AlertError("down", 502)
        self.sent.append((str(chat), text))

    def __call__(self, chat, text):
        return self.send(chat, text)


class Base(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "a.db")
        seed(self.path)
        self.w = web.WriteDB(self.path)
        self.r = web.ReadDB(self.path)
        credits.credit_deposit(self.w, "0xt", 0, ME, 20_000_000, 1)

    def tearDown(self):
        self.w.close()
        self.r.close()

    def subscribe(self, link=True):
        alerts.buy(self.w, ME, ST)
        if link:
            code, _ = alerts.new_link_code(self.w, ME)
            self.assertEqual(alerts.link_chat(self.w, code, 4242), ME)


class PlanAndWatchTests(Base):
    def test_watching_needs_a_plan(self):
        with self.assertRaises(alerts.AlertError) as cm:
            alerts.add_watch(self.w, self.r, ME, "token", A)
        self.assertEqual(cm.exception.status, 402)

    def test_buy_charges_and_refuses_without_credits(self):
        exp = alerts.buy(self.w, ME, ST, now=1000)
        self.assertEqual(exp, 1000 + 30 * 86400)
        self.assertEqual(credits.balance(self.w, ME), 15_000_000)
        with self.assertRaises(alerts.AlertError):
            alerts.buy(self.w, "0x" + "88" * 20, ST)

    def test_watch_validation_and_limit(self):
        self.subscribe()
        for kind, target, th in (("nft", A, 20), ("token", "nope", 20), ("token", "0x" + "99" * 20, 20),
                                 ("token", A, 2), ("token", A, "abc")):
            with self.assertRaises(alerts.AlertError):
                alerts.add_watch(self.w, self.r, ME, kind, target, th)
        alerts.add_watch(self.w, self.r, ME, "token", A, 30)
        alerts.add_watch(self.w, self.r, ME, "token", A, 40)         # same watch: updated, not duplicated
        self.assertEqual(len(alerts.overview(self.w, self.r, ME)["watches"]), 1)
        for i in range(alerts.MAX_WATCHES - 1):
            alerts.add_watch(self.w, self.r, ME, "wallet", "0x" + f"{i + 1:02x}" * 20)
        with self.assertRaises(alerts.AlertError):
            alerts.add_watch(self.w, self.r, ME, "wallet", "0x" + "ee" * 20)

    def test_users_only_remove_their_own_watches(self):
        self.subscribe()
        alerts.add_watch(self.w, self.r, ME, "token", A)
        wid = alerts.overview(self.w, self.r, ME)["watches"][0]["id"]
        alerts.remove_watch(self.w, "0x" + "88" * 20, wid)
        self.assertEqual(len(alerts.overview(self.w, self.r, ME)["watches"]), 1)
        alerts.remove_watch(self.w, ME, wid)
        self.assertEqual(alerts.overview(self.w, self.r, ME)["watches"], [])


class LinkTests(Base):
    def test_code_is_single_use_and_expires(self):
        code, exp = alerts.new_link_code(self.w, ME, now=1000)
        self.assertIsNone(alerts.link_chat(self.w, code, 1, now=exp + 1))
        code, exp = alerts.new_link_code(self.w, ME, now=1000)
        self.assertEqual(alerts.link_chat(self.w, code.lower(), 7, now=1001), ME)
        self.assertIsNone(alerts.link_chat(self.w, code, 8, now=1002))
        for bad in ("", "short", "ZZZZZZZZ", None):
            self.assertIsNone(alerts.link_chat(self.w, bad, 1))

    def test_new_code_replaces_the_old_one(self):
        old, _ = alerts.new_link_code(self.w, ME)
        new, _ = alerts.new_link_code(self.w, ME)
        self.assertIsNone(alerts.link_chat(self.w, old, 1))
        self.assertEqual(alerts.link_chat(self.w, new, 1), ME)

    def test_bot_messages(self):
        code, _ = alerts.new_link_code(self.w, ME)
        tg = FakeTg()
        ups = [{"update_id": 5, "message": {"chat": {"id": 9, "type": "private"}, "text": "/start " + code}},
               {"update_id": 6, "message": {"chat": {"id": 10, "type": "private"}, "text": "hello"}},
               {"update_id": 7, "message": {"chat": {"id": -5, "type": "group"}, "text": code}}]
        nxt = alerts.handle_updates(self.w, tg, ups)
        self.assertEqual(nxt, 8)
        self.assertIn("Linked", tg.sent[0][1])
        self.assertIn("code", tg.sent[1][1])
        self.assertEqual(len(tg.sent), 2)                                # nothing sent to the group


class CheckTests(Base):
    def test_token_alert_fires_once_per_cooldown(self):
        self.subscribe()
        alerts.add_watch(self.w, self.r, ME, "token", A, 50)
        tg = FakeTg()
        self.assertEqual(alerts.check(self.w, self.r, tg, now=NOW), 1)
        chat, text = tg.sent[0]
        self.assertEqual(chat, "4242")
        self.assertIn("AAA moved", text)
        self.assertIn("not financial advice", text)
        self.assertEqual(alerts.check(self.w, self.r, tg, now=NOW + 60), 0)                 # cooldown
        self.assertEqual(alerts.check(self.w, self.r, tg, now=NOW + alerts.TOKEN_COOLDOWN + 61), 1)

    def test_threshold_not_reached_means_no_alert(self):
        self.subscribe()
        alerts.add_watch(self.w, self.r, ME, "token", A, 900)
        self.assertEqual(alerts.check(self.w, self.r, FakeTg(), now=NOW), 0)

    def test_wallet_alert_reports_new_trades_only(self):
        self.subscribe()
        alerts.add_watch(self.w, self.r, ME, "wallet", WALLETS[1], now=NOW - 10_000)
        tg = FakeTg()
        self.assertEqual(alerts.check(self.w, self.r, tg, now=NOW), 1)
        self.assertIn("traded", tg.sent[0][1])
        self.assertIn("AAA", tg.sent[0][1])
        self.assertEqual(alerts.check(self.w, self.r, tg, now=NOW + 5), 0)                  # already reported

    def test_new_watch_does_not_replay_history(self):
        self.subscribe()
        alerts.add_watch(self.w, self.r, ME, "wallet", WALLETS[1], now=NOW + 100)
        self.assertEqual(alerts.check(self.w, self.r, FakeTg(), now=NOW + 200), 0)

    def test_expired_plan_or_unlinked_chat_sends_nothing(self):
        self.subscribe()
        alerts.add_watch(self.w, self.r, ME, "token", A, 50)
        self.assertEqual(alerts.check(self.w, self.r, FakeTg(), now=NOW + 31 * 86400), 0)
        other = "0x" + "66" * 20
        credits.credit_deposit(self.w, "0xu", 0, other, 20_000_000, 1)
        alerts.buy(self.w, other, ST)
        alerts.add_watch(self.w, self.r, other, "token", A, 50)
        tg = FakeTg()
        alerts.check(self.w, self.r, tg, now=NOW)
        self.assertEqual([c for c, _ in tg.sent], ["4242"])                                 # unlinked user got nothing

    def test_failed_send_does_not_stop_others_or_mark_sent(self):
        self.subscribe()
        alerts.add_watch(self.w, self.r, ME, "token", A, 50)
        self.assertEqual(alerts.check(self.w, self.r, FakeTg(fail=True), now=NOW), 0)
        self.assertEqual(alerts.check(self.w, self.r, FakeTg(), now=NOW + 1), 1)            # retried next pass

    def test_hostile_symbol_never_reaches_the_message(self):
        self.subscribe()
        alerts.add_watch(self.w, self.r, ME, "token", A, 50)
        self.w.conn.execute("UPDATE tokens SET symbol = ? WHERE token = ?", ("<b>FREE</b> https://evil.com\n", A))
        self.w.conn.commit()
        tg = FakeTg()
        alerts.check(self.w, self.r, tg, now=NOW)
        text = tg.sent[0][1]
        self.assertNotIn("://", text)
        self.assertNotIn("evil.com", text)               # no domain-looking text that a chat app would linkify
        self.assertNotIn("<", text)


class HttpTests(Base):
    def setUp(self):
        super().setUp()
        self.tok = "b" * 64
        self.w.conn.execute("INSERT INTO sessions VALUES (?,?,?)",
                            (__import__("hashlib").sha256(self.tok.encode()).hexdigest(), ME, 9_999_999_999))
        self.w.conn.commit()
        p = mock.patch.dict(os.environ, {"TOKEN_ADDRESS": ST["token"], "TREASURY_ADDRESS": ST["treasury"],
                                         "TELEGRAM_BOT_TOKEN": "123:abc"})
        p.start()
        self.addCleanup(p.stop)

    def post(self, action, body, ip, tok=True):
        return web.post_alerts(self.path, action, json.dumps(body).encode(), ip, self.tok if tok else None)

    def test_flow(self):
        self.assertEqual(self.post("me", {}, "8.8.1.1", tok=False)[0], 401)
        self.assertEqual(self.post("watch", {"kind": "token", "target": A}, "8.8.1.2")[0], 402)
        st, out = self.post("buy", {}, "8.8.1.3")
        self.assertEqual((st, out["balance"]), (200, "15"))
        st, out = self.post("link", {}, "8.8.1.4")
        self.assertEqual((st, len(out["code"])), (200, 8))
        st, out = self.post("watch", {"kind": "token", "target": A, "threshold": 25}, "8.8.1.5")
        self.assertEqual((st, out["watches"][0]["label"], out["watches"][0]["threshold"]), (200, "AAA", 25.0))
        self.assertEqual(self.post("nonsense", {}, "8.8.1.6")[0], 404)
        self.assertEqual(self.post("unwatch", {"id": "x"}, "8.8.1.7")[0], 400)
        st, cfg_ = web.route(self.path, "/api/alerts/config", {})
        self.assertEqual((cfg_["enabled"], cfg_["price"], cfg_["days"]), (True, "5", 30))

    def test_link_unavailable_without_telegram_token(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}):
            self.assertEqual(self.post("link", {}, "8.8.2.1")[0], 404)


if __name__ == "__main__":
    unittest.main()
