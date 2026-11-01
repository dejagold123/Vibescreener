"""Holder perks: wallets that hold enough of the token get more free questions.

Read-only: the balance comes from the token contract through the RPC node (balanceOf), nothing is
spent or locked. Results are cached for a few minutes, and any failure counts as "not a holder"
so an RPC hiccup can only ever withhold a perk, never grant one."""
import time
from decimal import Decimal

from . import config as cfg
from .rpc import Rpc, RpcError

TTL = 300
FAIL_TTL = 60
_cache = {}
_decimals = {}


def _rpc():
    return Rpc(cfg.resolve_rpc_url(), timeout=6, retries=1)


def balance_of(token, wallet, rpc=None, now=None):
    """Token balance of `wallet` as a Decimal, or None when it can't be read."""
    now = time.time() if now is None else now
    hit = _cache.get((token, wallet))
    if hit and now - hit[0] < (TTL if hit[1] is not None else FAIL_TTL):
        return hit[1]
    rpc = rpc or _rpc()
    try:
        if token not in _decimals:
            _decimals[token] = int(rpc.call("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"]), 16)
        raw = rpc.call("eth_call", [{"to": token, "data": "0x70a08231" + wallet[2:].lower().rjust(64, "0")},
                                    "latest"])
        value = Decimal(int(raw, 16)) / Decimal(10) ** min(_decimals[token], 36)
    except (RpcError, ValueError, TypeError, KeyError):
        _cache[(token, wallet)] = (now, None)               # don't hammer a node that is down
        return None
    if len(_cache) > 5000:
        _cache.clear()
    _cache[(token, wallet)] = (now, value)
    return value


def tier(wallet, settings=None, rpc=None, now=None):
    """{'holder': bool, 'balance': 'text', 'min': 'text', 'free': n} for a signed-in wallet."""
    st = settings or cfg.credits_settings()
    out = {"holder": False, "balance": None, "min": st["holder_min"], "free": st["holder_free"]}
    if not st["enabled"] or not wallet:
        return out
    bal = balance_of(st["token"], wallet, rpc=rpc, now=now)
    if bal is None:
        return out
    out["balance"] = format(bal.quantize(Decimal("0.01")), "f") if bal < Decimal(10) ** 12 else "1e12+"
    out["holder"] = bal >= Decimal(st["holder_min"]) and Decimal(st["holder_min"]) > 0
    return out
