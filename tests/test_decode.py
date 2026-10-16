"""Fixtures are the real logs of three testnet transactions (hex words copied
from the explorer). Expected decimals are the values the explorer decoded for
the same logs, so a wrong layout or a mistyped word fails an assertion."""
import datetime
import sqlite3
import unittest

from vibescreener import config as cfg
from vibescreener.db import DB
from vibescreener.decode import (Log, decode_curve, decode_launch, decode_router, pair_trades)
from vibescreener.indexer import Indexer

TRADER = "0x03811ed81bcadded400d8e9fa7692bce8ca413b1"
ROUTER = cfg.ROUTER
BTX = "0xedb8b4e85411ef6ef97e8fdcdbde7b921adbe209"
BTX_CURVE = "0x3964ba6f6ab5fc5a65107ec2241a5d54746ab88c"
JHJ = "0xbd158e4fde019e2f768a055397a2a83c9bf14d53"
JHJ_CURVE = "0x2413ce3cc15418df7b2e9b779825f2b3e29ce695"

TX_BTX_BUY = "0xd0bc169b5570b7f14244d8f2c5623297993bf863adfb163380f45a63a462f9c5"
TX_SELL = "0xd937397adb0922bf0803160fa1907d54fffff4645c26e3454f4307c49717a978"
TX_JHJ_BUY = "0xc467abeba0efd45495b2674eea56254eb34d60d8bf5c4d6438215bbcb843d53c"
TX_LAUNCH = "0x0b9c8bb89b52140e8104f85b48c9694f8e05203c27108eb1dad3d851e3aa8601"

URI = "ipfs://bafkreicsmbhmueyqxp4q2zcrehdprfdznl4o4335r5mjiukb5oqbcdqgxa"


def word(x):
    if isinstance(x, str):
        x = int(x, 16)
    return f"{x:064x}"


def data(*ws):
    return "0x" + "".join(word(w) for w in ws)


def t_addr(a):
    return "0x" + a.replace("0x", "").rjust(64, "0")


def ts(iso):
    return int(datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def launch_data(curve, quote, creator, name, symbol, uri):
    head = [curve, 0x1a0, 0x1e0, 0x220, quote, 0xc8, 0x1f41d4c07d0, creator,
            0x23dcda1812048bee8a5, 0, 0, 0, 0]

    def dyn(s):
        b = s.encode()
        return word(len(b)) + (b + b"\0" * (-len(b) % 32)).hex()
    return "0x" + "".join(word(w) for w in head) + dyn(name) + dyn(symbol) + dyn(uri)


# --- BTX buy (block 136197968) -----------------------------------------------
BTX_BUY_CURVE = Log(BTX_CURVE, (cfg.TOPIC_CURVE_BUY, t_addr(ROUTER), t_addr(TRADER)),
                    data("66db61026549bb43e13", "658a5658ca643b1ef75", "1510aa99ae58024e9e",
                         "1719d99a934eb", "2a946889751", 0xc8), 136197968, 4, TX_BTX_BUY)
BTX_BUY_ROUTER = Log(ROUTER, (cfg.TOPIC_ROUTER, t_addr(TRADER), t_addr(BTX_CURVE)),
                     data(1, 0, BTX, "17446e031cc3d", "17446e031cc3d"), 136197968, 5, TX_BTX_BUY)

# --- BTX sell (block 136202181) ----------------------------------------------
SELL_CURVE = Log(BTX_CURVE, (cfg.TOPIC_CURVE_SELL, t_addr(TRADER), t_addr(TRADER)),
                 data("1962959632990ec7bdd", "190f67068db35c6a40f", "532e8fa4e5b25d7ce",
                      "5a1940d013ba", "a611dfe934"), 136202181, 13, TX_SELL)
SELL_ROUTER = Log(ROUTER, (cfg.TOPIC_ROUTER, t_addr(TRADER), t_addr(BTX_CURVE)),
                  data(2, BTX, 0, "1962959632990ec7bdd", "59732ef02a86"), 136202181, 14, TX_SELL)

# --- JHJ buy, quote token is VIBEVIBE (block 136201882) ------------------------
JHJ_BUY_CURVE = Log(JHJ_CURVE, (cfg.TOPIC_CURVE_BUY, t_addr(ROUTER), t_addr(TRADER)),
                    data("863344ea8db978b19178", "847b8591f1e69c6c1ef8", "1b7bf589bd2dc457280",
                         "1d4573281b0c46df", "35f3ed3e0b5a36", 0xc8), 136201882, 16, TX_JHJ_BUY)
JHJ_BUY_ROUTER = Log(ROUTER, (cfg.TOPIC_ROUTER, t_addr(TRADER), t_addr(JHJ_CURVE)),
                     data(1, 0, JHJ, "2e8b33547d5c2", "1d7b67155917a116"), 136201882, 18, TX_JHJ_BUY)

# --- JHJ launch (block 136201571) ----------------------------------------------
LAUNCH = Log(cfg.FACTORY, (cfg.TOPIC_LAUNCH, "0x" + f"{0x3598:064x}", t_addr(TRADER), t_addr(JHJ)),
             launch_data(JHJ_CURVE, cfg.VIBEVIBE_TOKEN, TRADER, "Jubberish", "JHJ", URI),
             136201571, 25, TX_LAUNCH)
LAUNCH_ROUTER = Log(ROUTER, (cfg.TOPIC_ROUTER, t_addr(TRADER), t_addr(JHJ_CURVE)),
                    data(0, 0, JHJ, 5, 0), 136201571, 27, TX_LAUNCH)

ALL_LOGS = [LAUNCH, LAUNCH_ROUTER, BTX_BUY_CURVE, BTX_BUY_ROUTER, JHJ_BUY_CURVE,
            JHJ_BUY_ROUTER, SELL_CURVE, SELL_ROUTER]

BLOCK_TS = {136197968: ts("2026-10-16T00:12:02Z"), 136201571: ts("2026-10-16T00:19:05Z"),
            136201882: ts("2026-10-16T00:19:41Z"), 136202181: ts("2026-10-16T00:20:16Z")}


class FakeRpc:
    def get_block_timestamps(self, blocks):
        return {b: BLOCK_TS[b] for b in blocks}


class DecodeTests(unittest.TestCase):
    def test_launch(self):
        d = decode_launch(LAUNCH)
        self.assertEqual(d["name"], "Jubberish")
        self.assertEqual(d["symbol"], "JHJ")
        self.assertEqual(d["uri"], URI)
        self.assertEqual(len(URI.encode()), 0x42)  # length word seen on chain
        self.assertEqual(d["token"], JHJ)
        self.assertEqual(d["curve"], JHJ_CURVE)
        self.assertEqual(d["creator"], TRADER)
        self.assertEqual(d["quote_token"], cfg.VIBEVIBE_TOKEN)
        self.assertEqual(d["launch_id"], 13720)

    def test_curve_buy_matches_explorer_values(self):
        c = decode_curve(BTX_BUY_CURVE)
        self.assertEqual(c["side"], "buy")
        self.assertEqual(c["gross_tokens"], 30358013029111574052371)
        self.assertEqual(c["tokens"], 29969430462338945904501)
        self.assertEqual(c["token_fee"], 388582566772628147870)
        self.assertEqual(c["reserve_after"], 406396678517995)
        self.assertEqual(c["quote_fee"], 2926056085329)
        self.assertEqual(c["gross_tokens"], c["tokens"] + c["token_fee"])
        # reserve + quote fee == ETH paid (tx value 409322734603325) within 1 wei
        self.assertLessEqual(abs(c["reserve_after"] + c["quote_fee"] - 409322734603325), 1)
        self.assertEqual(c["account"], TRADER)

    def test_curve_buy_second_token(self):
        c = decode_curve(JHJ_BUY_CURVE)
        self.assertEqual(c["gross_tokens"], 633742858589658508530040)
        self.assertEqual(c["tokens"], 625630949999710879620856)
        self.assertEqual(c["token_fee"], 8111908589947628909184)
        self.assertEqual(c["reserve_after"], 2109218616582620895)
        self.assertEqual(c["quote_fee"], 15186374039394870)
        self.assertLessEqual(abs(c["reserve_after"] + c["quote_fee"] - 2124404990622015766), 1)

    def test_curve_sell(self):
        c = decode_curve(SELL_CURVE)
        self.assertEqual(c["side"], "sell")
        self.assertEqual(c["tokens"], 7492357615584736476125)
        self.assertEqual(c["token_fee"], 95902177479484626894)
        self.assertEqual(c["tokens_after_fee"], 7396455438105251849231)
        self.assertEqual(c["tokens"] - c["token_fee"], c["tokens_after_fee"])
        self.assertEqual(c["reserve_after"], 99064508060602)
        self.assertEqual(c["quote_fee"], 713264458036)

    def test_router_events(self):
        buy = decode_router(BTX_BUY_ROUTER)
        self.assertEqual((buy["kind"], buy["token_out"], buy["a"]), (1, BTX, 409322734603325))
        jhj = decode_router(JHJ_BUY_ROUTER)
        self.assertEqual(jhj["a"], 818806344111554)          # == tx value
        self.assertEqual(jhj["b"], 2124404990622015766)      # VIBEVIBE amount after swap
        self.assertEqual(decode_router(LAUNCH_ROUTER)["kind"], 0)

    def test_pairing(self):
        routers = [decode_router(l) for l in ALL_LOGS if l.address == ROUTER]
        curves = [decode_curve(l) for l in ALL_LOGS if l.address in (BTX_CURVE, JHJ_CURVE)]
        trades, unpaired, skipped = pair_trades(routers, curves)
        self.assertEqual(unpaired, 0)
        self.assertEqual(dict(skipped), {0: 1})  # the launch router event
        by_tx = {t["tx"]: t for t in trades}
        self.assertEqual(len(by_tx), 3)

        b = by_tx[TX_BTX_BUY]
        self.assertEqual((b["side"], b["token"], b["trader"]), ("buy", BTX, TRADER))
        self.assertEqual(b["token_amount"], 29969430462338945904501)  # matches ERC-20 transfer to buyer
        self.assertEqual(b["native_in"], 409322734603325)

        s = by_tx[TX_SELL]
        self.assertEqual((s["side"], s["token"]), ("sell", BTX))
        self.assertEqual(s["token_amount"], 7492357615584736476125)
        self.assertEqual(s["token_fee"], 95902177479484626894)
        self.assertEqual(s["quote_amount"], 0x59732ef02a86)
        # gross quote ~ reserve-neutral: quote out + quote fee is just under the reserve left behind
        self.assertGreater(s["quote_amount"] + s["quote_fee"], 0)

        j = by_tx[TX_JHJ_BUY]
        self.assertEqual((j["side"], j["token"]), ("buy", JHJ))
        self.assertEqual(j["token_amount"], 625630949999710879620856)  # matches ERC-20 transfer to buyer
        self.assertEqual(j["quote_amount"], 2124404990622015766)
        self.assertEqual(j["native_in"], 818806344111554)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.idx = Indexer(FakeRpc(), self.db, log=lambda *_: None)

    def test_ingest_and_views(self):
        st = self.idx.ingest(ALL_LOGS)
        self.assertEqual(st["launches"], 1)
        self.assertEqual(st["trades"], 3)
        self.assertEqual(st["unpaired"], 0)

        tok = self.db.q("SELECT * FROM tokens")
        self.assertEqual([t["symbol"] for t in tok], ["JHJ"])
        self.assertEqual(tok[0]["ts"], BLOCK_TS[136201571])

        trades = self.db.q("SELECT * FROM trades ORDER BY block")
        self.assertEqual([t["side"] for t in trades], ["buy", "buy", "sell"])
        self.assertEqual(trades[0]["token_amount"], "29969430462338945904501")  # exact, not rounded
        self.assertEqual(trades[0]["ts"], BLOCK_TS[136197968])

        # idempotent: re-ingesting the same logs adds nothing
        self.idx.ingest(ALL_LOGS)
        self.assertEqual(self.db.q("SELECT COUNT(*) n FROM trades")[0]["n"], 3)

        # views run and group sensibly: JHJ (VIBEVIBE-quoted) and BTX (launch not indexed -> NULL quote)
        pnl = {r["quote_token"]: r for r in self.db.q("SELECT * FROM wallet_pnl")}
        self.assertEqual(set(pnl), {cfg.VIBEVIBE_TOKEN, None})
        self.assertEqual(pnl[None]["trades"], 2)               # BTX buy + sell
        self.assertGreater(pnl[None]["quote_received"], 0)
        self.assertEqual(self.db.q("SELECT creator, tokens_launched, unique_traders FROM creator_stats"),
                         [{"creator": TRADER, "tokens_launched": 1, "unique_traders": 1}])
        stats = self.db.q("SELECT symbol, trades, last_price FROM token_stats")
        self.assertEqual(stats[0]["symbol"], "JHJ")
        self.assertGreater(stats[0]["last_price"], 0)


if __name__ == "__main__":
    unittest.main()
