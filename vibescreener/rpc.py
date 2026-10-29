"""Minimal JSON-RPC client with retries and eth_getLogs helpers."""
import re
import time

import requests

from .decode import Log

# NOTE (M11): "timeout"/"timed out" are deliberately NOT range hints. A
# timeout is a transient failure, not proof the range was too large; treating
# it as one made backfill halve its chunk for a slow node and then oscillate.
RANGE_HINTS = ("range", "limit", "too many", "exceed", "more than", "response size",
               "10000")


_KEY_IN_URL = re.compile(r"(/v2/)[A-Za-z0-9_\-]{6,}")


def redact(text):
    """Hide API keys that appear in URLs (requests puts the full URL in its errors),
    so they never reach logs such as Railway's."""
    return _KEY_IN_URL.sub(r"\1***", str(text))


class RpcError(Exception):
    def __init__(self, message=""):
        super().__init__(redact(message))


class RpcAuthError(RpcError):
    """The node rejected our credentials (401/403). Retrying cannot help."""


class RangeTooLarge(RpcError):
    """The node refused the query because the block range / result was too big."""


class Rpc:
    def __init__(self, url, timeout=30, retries=4):
        self.url, self.timeout, self.retries = url, timeout, retries
        self.s = requests.Session()
        self._id = 0

    def _post(self, payload):
        last = None
        for attempt in range(self.retries):
            try:
                r = self.s.post(self.url, json=payload, timeout=self.timeout)
                if r.status_code in (429, 502, 503, 504):
                    raise RpcError(f"http {r.status_code}")
                if r.status_code == 400:
                    # Alchemy (and some other nodes) return 400 with a JSON
                    # error body when the block range is too large rather than
                    # a retryable 429.  Detect that case so the backfill loop
                    # can halve the chunk size instead of giving up.
                    try:
                        msg = str(r.json().get("error", {}).get("message", "")).lower()
                    except ValueError:
                        msg = r.text.lower()
                    if any(h in msg for h in RANGE_HINTS) or "block range" in msg or "tier" in msg:
                        raise RangeTooLarge(f"400 range too large: {msg[:120]}")
                if r.status_code in (401, 403):
                    raise RpcAuthError(f"http {r.status_code}: the node rejected the API key")
                r.raise_for_status()
                return r.json()
            except requests.Timeout as e:
                last = e
            except (RangeTooLarge, RpcAuthError):
                raise  # don't retry; let backfill halve the chunk / surface the bad key
            except (requests.RequestException, ValueError, RpcError) as e:
                last = e
            time.sleep(min(2 ** attempt, 10))
        if isinstance(last, requests.Timeout):
            # M11: timeouts are transient retries, not range errors.
            raise RpcError(f"timeout after {self.retries} tries: {last}")
        raise RpcError(f"request failed after {self.retries} tries: {last}")

    def call(self, method, params=None):
        self._id += 1
        body = self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or []})
        if "error" in body:
            msg = str(body["error"]).lower()
            if any(h in msg for h in RANGE_HINTS):
                raise RangeTooLarge(msg)
            raise RpcError(msg)
        return body["result"]

    def block_number(self):
        return int(self.call("eth_blockNumber"), 16)

    def get_logs(self, lo, hi, address=None, topics=None):
        flt = {"fromBlock": hex(lo), "toBlock": hex(hi)}
        if address:
            flt["address"] = address
        if topics:
            flt["topics"] = topics
        return [Log.from_rpc(d) for d in self.call("eth_getLogs", [flt])]

    def get_receipt_logs(self, tx_hash):
        rc = self.call("eth_getTransactionReceipt", [tx_hash])
        if not rc:
            raise RpcError(f"no receipt for {tx_hash}")
        return [Log.from_rpc(d) for d in rc["logs"]]

    def get_block_timestamps(self, blocks):
        blocks = sorted(set(blocks))
        out = {}
        for i in range(0, len(blocks), 50):
            part = blocks[i:i + 50]
            batch = [{"jsonrpc": "2.0", "id": n, "method": "eth_getBlockByNumber",
                      "params": [hex(b), False]} for n, b in enumerate(part)]
            try:
                res = self._post(batch)
                for item in res:
                    b = part[item["id"]]
                    # M15: a future/missing block returns result=None; skip it
                    # instead of crashing the caller with TypeError/KeyError.
                    result = item.get("result") if isinstance(item, dict) else None
                    if result is None or "timestamp" not in result:
                        continue
                    out[b] = int(result["timestamp"], 16)
            except (RpcError, KeyError, TypeError):
                for b in part:  # node may not support batches
                    try:
                        blk = self.call("eth_getBlockByNumber", [hex(b), False])
                    except (RpcError, KeyError, TypeError):
                        continue
                    if blk is None or "timestamp" not in blk:
                        continue
                    out[b] = int(blk["timestamp"], 16)
        return out
