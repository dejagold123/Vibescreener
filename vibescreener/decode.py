"""Decoders for the (unverified) vibe/vibe contracts.

Layouts were reverse-engineered from three real transactions (a launch, a buy
and a sell) and cross-checked against ERC-20 Transfer logs. Fields marked
"unknown" are decoded but their meaning is not confirmed.

Factory launch event (topic0 = TOPIC_LAUNCH)
    topics: [t0, launch_id, creator, token]
    data (13-word head, then dynamic strings):
        w0 curve address | w1,w2,w3 offsets of name, symbol, uri |
        w4 quote token   | w5 unknown (200) | w6 unknown | w7 creator |
        w8 unknown       | w9..w12 zero | name | symbol | uri

Router trade event (topic0 = TOPIC_ROUTER), one per user trade
    topics: [t0, trader, curve]
    data: w0 kind (0 = launch, 1 = buy, 2 = sell)
          w1 token in  (0 = native ETH)
          w2 token out (0 = native ETH)
          buy : w3 ETH paid by the user, w4 quote amount fed to the curve
                (equals w3 for ETH-quoted tokens; for VIBEVIBE-quoted tokens
                it is the VIBEVIBE received from the ETH->VIBEVIBE swap)
          sell: w3 tokens sold, w4 quote amount paid out

Curve buy event (topic0 = TOPIC_CURVE_BUY), emitted by the per-token curve
    topics: [t0, router, buyer]
    data: gross_tokens, tokens_to_buyer, token_fee, reserve_after,
          quote_fee, unknown(200)
    Checked: gross = to_buyer + fee, and reserve_after + quote_fee == quote in
    (to within 1 wei) on two first-ever buys.

Curve sell event (topic0 = TOPIC_CURVE_SELL)
    topics: [t0, seller, seller]
    data: tokens_in, tokens_after_fee, token_fee, reserve_after, quote_fee
    Checked: tokens_after_fee == tokens_in - token_fee.

Blockscout labels these events with misleading names (CurveBuy/CurveSell
parameters); the layouts above follow the numbers, not the labels.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Optional

from . import config as cfg


@dataclass(frozen=True)
class Log:
    address: str
    topics: tuple
    data: str
    block: int
    log_index: int
    tx: str

    @staticmethod
    def from_rpc(d: dict) -> "Log":
        return Log(
            address=d["address"].lower(),
            topics=tuple(t.lower() for t in d["topics"]),
            data=d["data"].lower(),
            block=int(d["blockNumber"], 16),
            log_index=int(d["logIndex"], 16),
            tx=d["transactionHash"].lower(),
        )


def _words(data: str) -> list:
    raw = data[2:] if data.startswith("0x") else data
    return [int(raw[i:i + 64], 16) for i in range(0, len(raw) - len(raw) % 64, 64)]


def _addr_word(w: int) -> str:
    return "0x" + f"{w:040x}"


def _addr_topic(t: str) -> str:
    return "0x" + t[-40:].lower()


def _read_string(data_bytes: bytes, offset: int) -> str:
    if offset < 0 or offset + 32 > len(data_bytes):
        raise ValueError("string offset out of bounds")
    length = int.from_bytes(data_bytes[offset:offset + 32], "big")
    if length < 0 or offset + 32 + length > len(data_bytes):
        raise ValueError("string length out of bounds")
    return data_bytes[offset + 32: offset + 32 + length].decode("utf-8", "replace")


def decode_launch(lg: Log) -> Optional[dict]:
    """Return the launch dict, or None for a malformed log (short topics/data
    or out-of-bounds string offsets). Callers must skip None, never crash."""
    try:
        if len(lg.topics) < 4:
            return None
        w = _words(lg.data)
        if len(w) < 9:
            return None
        raw = bytes.fromhex(lg.data[2:] if lg.data.startswith("0x") else lg.data)
        return {
            "tx": lg.tx, "log_index": lg.log_index, "block": lg.block,
            "launch_id": int(lg.topics[1], 16),
            "creator": _addr_topic(lg.topics[2]),
            "token": _addr_topic(lg.topics[3]),
            "curve": _addr_word(w[0]),
            "quote_token": _addr_word(w[4]),
            "name": _read_string(raw, w[1]),
            "symbol": _read_string(raw, w[2]),
            "uri": _read_string(raw, w[3]),
            "extra": {"w5": w[5], "w6": w[6], "w8": w[8]},
        }
    except (IndexError, ValueError):
        return None


def decode_router(lg: Log) -> Optional[dict]:
    try:
        if len(lg.topics) < 3:
            return None
        w = _words(lg.data)
        if len(w) < 5:
            return None
        return {
            "tx": lg.tx, "log_index": lg.log_index, "block": lg.block,
            "trader": _addr_topic(lg.topics[1]),
            "curve": _addr_topic(lg.topics[2]),
            "kind": w[0],
            "token_in": _addr_word(w[1]),
            "token_out": _addr_word(w[2]),
            "a": w[3],
            "b": w[4],
        }
    except (IndexError, ValueError):
        return None


def decode_curve(lg: Log) -> Optional[dict]:
    try:
        if len(lg.topics) < 3:
            return None
        w = _words(lg.data)
        if len(w) < 5:
            return None
        t0 = lg.topics[0]
        base = {"tx": lg.tx, "log_index": lg.log_index, "block": lg.block,
                "address": lg.address, "account": _addr_topic(lg.topics[2])}
        if t0 == cfg.TOPIC_CURVE_BUY:
            return {**base, "side": "buy", "gross_tokens": w[0], "tokens": w[1],
                    "token_fee": w[2], "reserve_after": w[3], "quote_fee": w[4]}
        if t0 == cfg.TOPIC_CURVE_SELL:
            return {**base, "side": "sell", "gross_tokens": w[0], "tokens": w[0],
                    "tokens_after_fee": w[1], "token_fee": w[2],
                    "reserve_after": w[3], "quote_fee": w[4]}
        return None
    except (IndexError, ValueError):
        return None


def pair_trades(routers: list, curves: list):
    """Join each router trade event with the curve event that precedes it in
    the same transaction. Returns (trades, unpaired_count, skipped_kinds)."""
    by_tx = defaultdict(list)
    for c in curves:
        by_tx[c["tx"]].append(c)
    used = set()
    trades, unpaired, skipped = [], 0, Counter()
    for r in sorted(routers, key=lambda r: (r["tx"], r["log_index"])):
        if r["kind"] not in (1, 2):
            skipped[r["kind"]] += 1
            continue
        side = "buy" if r["kind"] == 1 else "sell"
        cands = [c for c in by_tx[r["tx"]]
                 if c["side"] == side and c["address"] == r["curve"]
                 and c["log_index"] < r["log_index"]
                 and (c["tx"], c["log_index"]) not in used]
        c = max(cands, key=lambda c: c["log_index"]) if cands else None
        if c:
            used.add((c["tx"], c["log_index"]))
        else:
            unpaired += 1
        token = r["token_out"] if side == "buy" else r["token_in"]
        # Amount asymmetry is intentional for position accounting: a buy
        # credits the net tokens actually received, while a sell debits the
        # gross tokens sold (the fee is taken from that gross amount, so
        # selling everything you bought nets to zero). Prices on both sides
        # therefore include fees. Unpaired buys get token_amount=None and
        # are counted separately (see web.pairs/overview unpaired counts).
        trade = {
            "tx": r["tx"], "log_index": r["log_index"], "block": r["block"],
            "side": side, "trader": r["trader"], "token": token, "curve": r["curve"],
            "quote_amount": r["b"],
            "native_in": r["a"] if side == "buy" else None,
            "token_amount": (c["tokens"] if c else None) if side == "buy" else r["a"],
            "token_fee": c["token_fee"] if c else None,
            "quote_fee": c["quote_fee"] if c else None,
            "reserve_after": c["reserve_after"] if c else None,
        }
        trades.append(trade)
    return trades, unpaired, skipped
