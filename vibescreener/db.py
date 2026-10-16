"""SQLite storage. Raw on-chain amounts are kept as exact TEXT integers (wei
amounts overflow SQLite's 64-bit ints); *_amt columns are float copies in whole
units (value / 1e18) used by the analytics views."""
import json
import sqlite3

from . import config as cfg

SCHEMA = """
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS blocks(block INTEGER PRIMARY KEY, ts INTEGER);
CREATE TABLE IF NOT EXISTS tokens(
  token TEXT PRIMARY KEY, curve TEXT, creator TEXT, name TEXT, symbol TEXT, uri TEXT,
  quote_token TEXT, launch_id INTEGER, block INTEGER, ts INTEGER, tx TEXT,
  log_index INTEGER, extra TEXT);
CREATE TABLE IF NOT EXISTS trades(
  tx TEXT, log_index INTEGER, block INTEGER, ts INTEGER, side TEXT, trader TEXT,
  token TEXT, curve TEXT, token_amount TEXT, token_fee TEXT, quote_amount TEXT,
  quote_fee TEXT, reserve_after TEXT, native_in TEXT, token_amt REAL, quote_amt REAL,
  PRIMARY KEY(tx, log_index));
CREATE INDEX IF NOT EXISTS trades_trader ON trades(trader);
CREATE INDEX IF NOT EXISTS trades_token ON trades(token, block);
CREATE INDEX IF NOT EXISTS tokens_creator ON tokens(creator);

-- Last trade price per token, in quote units per token (approximate: ignores fees).
CREATE VIEW IF NOT EXISTS token_last_price AS
SELECT t.token, t.quote_amt / t.token_amt AS price
FROM trades t
JOIN (SELECT token, MAX(block * 100000 + log_index) AS k
      FROM trades WHERE token_amt > 0 GROUP BY token) m
  ON m.token = t.token AND (t.block * 100000 + t.log_index) = m.k;

-- One row per wallet and token. Amounts are in whole units of the token / of its quote token.
CREATE VIEW IF NOT EXISTS wallet_token_pnl AS
SELECT tr.trader, tr.token, k.symbol, k.quote_token,
  SUM(CASE WHEN tr.side = 'buy'  THEN tr.token_amt ELSE 0 END) AS tokens_bought,
  SUM(CASE WHEN tr.side = 'sell' THEN tr.token_amt ELSE 0 END) AS tokens_sold,
  SUM(CASE WHEN tr.side = 'buy'  THEN tr.quote_amt ELSE 0 END) AS quote_spent,
  SUM(CASE WHEN tr.side = 'sell' THEN tr.quote_amt ELSE 0 END) AS quote_received,
  COUNT(*) AS trades,
  p.price AS last_price
FROM trades tr
LEFT JOIN tokens k ON k.token = tr.token
LEFT JOIN token_last_price p ON p.token = tr.token
GROUP BY tr.trader, tr.token;

-- Wallet PnL per quote token (mixing quote tokens would add unlike units).
-- Unrealized part values remaining tokens at the last trade price.
-- Token transfers between wallets are not tracked, so treat this as an estimate.
CREATE VIEW IF NOT EXISTS wallet_pnl AS
SELECT trader, quote_token,
  COUNT(DISTINCT token) AS tokens_traded, SUM(trades) AS trades,
  SUM(quote_spent) AS quote_spent, SUM(quote_received) AS quote_received,
  SUM(quote_received + MAX(tokens_bought - tokens_sold, 0) * COALESCE(last_price, 0)
      - quote_spent) AS pnl_quote
FROM wallet_token_pnl GROUP BY trader, quote_token;

CREATE VIEW IF NOT EXISTS token_stats AS
SELECT k.token, k.symbol, k.name, k.creator, k.quote_token, k.ts AS launched_ts,
  COUNT(t.tx) AS trades, COUNT(DISTINCT t.trader) AS traders,
  COALESCE(SUM(t.quote_amt), 0) AS volume_quote, p.price AS last_price
FROM tokens k
LEFT JOIN trades t ON t.token = k.token
LEFT JOIN token_last_price p ON p.token = k.token
GROUP BY k.token;

CREATE VIEW IF NOT EXISTS creator_stats AS
SELECT k.creator, COUNT(DISTINCT k.token) AS tokens_launched,
  COUNT(t.tx) AS trades, COUNT(DISTINCT t.trader) AS unique_traders
FROM tokens k LEFT JOIN trades t ON t.token = k.token
GROUP BY k.creator;
"""


def _f(x):
    return None if x is None else x / cfg.WEI


def _s(x):
    return None if x is None else str(x)


class DB:
    def __init__(self, path=cfg.DB_PATH):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # cursor ----------------------------------------------------------
    def get_state(self, key, default=None):
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key, value):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, str(value)))

    # block timestamps ------------------------------------------------
    def get_block_ts(self, blocks):
        blocks = list(blocks)
        out = {}
        for i in range(0, len(blocks), 500):
            part = blocks[i:i + 500]
            q = ",".join("?" * len(part))
            for r in self.conn.execute(f"SELECT block, ts FROM blocks WHERE block IN ({q})", part):
                out[r["block"]] = r["ts"]
        return out

    def put_block_ts(self, mapping):
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO blocks VALUES (?,?)", mapping.items())

    # writes ----------------------------------------------------------
    def save(self, launches, trades, ts):
        with self.conn:
            for l in launches:
                self.conn.execute(
                    "INSERT OR REPLACE INTO tokens VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (l["token"], l["curve"], l["creator"], l["name"], l["symbol"], l["uri"],
                     l["quote_token"], l["launch_id"], l["block"], ts.get(l["block"]),
                     l["tx"], l["log_index"], json.dumps({k: str(v) for k, v in l["extra"].items()})))
            for t in trades:
                self.conn.execute(
                    "INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t["tx"], t["log_index"], t["block"], ts.get(t["block"]), t["side"],
                     t["trader"], t["token"], t["curve"], _s(t["token_amount"]), _s(t["token_fee"]),
                     _s(t["quote_amount"]), _s(t["quote_fee"]), _s(t["reserve_after"]),
                     _s(t["native_in"]), _f(t["token_amount"]), _f(t["quote_amount"])))

    def q(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params)]
