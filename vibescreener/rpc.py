"""Minimal JSON-RPC client with retries and eth_getLogs helpers."""
import time

import requests

from .decode import Log

RANGE_HINTS = ("range", "limit", "too many", "exceed", "more than", "response size",
               "10000", "timeout", "timed out")


class RpcError(Exception):
    pass


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
                r.raise_for_status()
                return r.json()
            except requests.Timeout as e:
                last = e
            except RangeTooLarge:
                raise  # don't retry; let backfill halve the chunk
            except (requests.RequestException, ValueError, RpcError) as e:
                last = e
            time.sleep(min(2 ** attempt, 10))
        if isinstance(last, requests.Timeout):
            raise RangeTooLarge(f"timeout: {last}")
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
                    out[b] = int(item["result"]["timestamp"], 16)
            except (RpcError, KeyError, TypeError):
                for b in part:  # node may not support batches
                    out[b] = int(self.call("eth_getBlockByNumber", [hex(b), False])["timestamp"], 16)
        return out
