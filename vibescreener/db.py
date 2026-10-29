"""SQLite storage. Raw on-chain amounts are kept as exact TEXT integers (wei
amounts overflow SQLite's 64-bit ints); *_amt columns are float copies in whole
units (value / 1e18) used by the analytics views."""
import json
import os
import sqlite3

from . import config as cfg

SCHEMA = """
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS blocks(block INTEGER PRIMARY KEY, ts INTEGER);
CREATE TABLE IF NOT EXISTS tokens(
  token TEXT PRIMARY KEY, curve TEXT, creator TEXT, name TEXT, symbol TEXT, uri TEXT,
  quote_token TEXT, launch_id INTEGER, block INTEGER, ts INTEGER, tx TEXT,
  log_index INTEGER, extra TEXT, decimals INTEGER NOT NULL DEFAULT 18);
CREATE TABLE IF NOT EXISTS trades(
  tx TEXT, log_index INTEGER, block INTEGER, ts INTEGER, side TEXT, trader TEXT,
  token TEXT, curve TEXT, token_amount TEXT, token_fee TEXT, quote_amount TEXT,
  quote_fee TEXT, reserve_after TEXT, native_in TEXT, token_amt REAL, quote_amt REAL,
  PRIMARY KEY(tx, log_index));
CREATE TABLE IF NOT EXISTS token_meta(
  token TEXT PRIMARY KEY, uri TEXT, description TEXT, image_cid TEXT, socials TEXT,
  status TEXT, attempts INTEGER, next_try INTEGER);
CREATE TABLE IF NOT EXISTS briefs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, text TEXT, facts TEXT, source TEXT, model TEXT);
CREATE TABLE IF NOT EXISTS ask_usage(day TEXT, key TEXT, n INTEGER, PRIMARY KEY(day, key));
CREATE TABLE IF NOT EXISTS auth_nonces(wallet TEXT PRIMARY KEY, nonce TEXT, expires INTEGER);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, wallet TEXT, expires INTEGER);
CREATE TABLE IF NOT EXISTS credits(wallet TEXT PRIMARY KEY, micro INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS deposits(
  tx TEXT, log_index INTEGER, wallet TEXT, micro INTEGER, ts INTEGER, PRIMARY KEY(tx, log_index));
CREATE INDEX IF NOT EXISTS trades_trader ON trades(trader);
CREATE INDEX IF NOT EXISTS trades_token ON trades(token, block);
CREATE INDEX IF NOT EXISTS tokens_creator ON tokens(creator);
CREATE INDEX IF NOT EXISTS trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS tokens_ts ON tokens(ts);

-- Last trade price per token, in quote units per token (approximate: ignores fees).
DROP VIEW IF EXISTS token_last_price;
CREATE VIEW token_last_price AS
SELECT t.token, t.quote_amt / t.token_amt AS price
FROM trades t
JOIN (SELECT token, MAX(block * 100000 + log_index) AS k
      FROM trades WHERE token_amt > 0 GROUP BY token) m
  ON m.token = t.token AND (t.block * 100000 + t.log_index) = m.k;

-- Views are dropped and recreated on every start so definition changes reach existing databases.
-- One row per wallet and token. Amounts are in whole units of the token / of its quote token.
-- complete = 0 when the wallet sold more than it was seen buying: the buy happened before
-- indexing began (or the tokens arrived by transfer), so the cost basis is missing.
DROP VIEW IF EXISTS wallet_token_pnl;
CREATE VIEW wallet_token_pnl AS
SELECT tr.trader, tr.token, k.symbol, k.quote_token,
  SUM(CASE WHEN tr.side = 'buy'  THEN tr.token_amt ELSE 0 END) AS tokens_bought,
  SUM(CASE WHEN tr.side = 'sell' THEN tr.token_amt ELSE 0 END) AS tokens_sold,
  SUM(CASE WHEN tr.side = 'buy'  THEN tr.quote_amt ELSE 0 END) AS quote_spent,
  SUM(CASE WHEN tr.side = 'sell' THEN tr.quote_amt ELSE 0 END) AS quote_received,
  COUNT(*) AS trades,
  p.price AS last_price,
  CASE WHEN SUM(CASE WHEN tr.side = 'sell' THEN tr.token_amt ELSE 0 END)
          <= SUM(CASE WHEN tr.side = 'buy' THEN tr.token_amt ELSE 0 END) * 1.0001
       THEN 1 ELSE 0 END AS complete
FROM trades tr
LEFT JOIN tokens k ON k.token = tr.token
LEFT JOIN token_last_price p ON p.token = tr.token
GROUP BY tr.trader, tr.token;

-- Wallet PnL per quote token (mixing quote tokens would add unlike units).
-- Only positions with a known quote token and a complete buy history are counted.
-- Unrealized part values remaining tokens at the last trade price.
-- Token transfers between wallets are not tracked, so treat this as an estimate.
DROP VIEW IF EXISTS wallet_pnl;
CREATE VIEW wallet_pnl AS
SELECT trader, quote_token,
  COUNT(DISTINCT token) AS tokens_traded, SUM(trades) AS trades,
  SUM(quote_spent) AS quote_spent, SUM(quote_received) AS quote_received,
  SUM(quote_received + MAX(tokens_bought - tokens_sold, 0) * COALESCE(last_price, 0)
      - quote_spent) AS pnl_quote
FROM wallet_token_pnl
WHERE complete = 1 AND quote_token IS NOT NULL
GROUP BY trader, quote_token;

-- How many wallet/token positions the PnL leaderboard leaves out, and why.
DROP VIEW IF EXISTS pnl_excluded;
CREATE VIEW pnl_excluded AS
SELECT
  SUM(CASE WHEN complete = 0 THEN 1 ELSE 0 END) AS incomplete_history,
  SUM(CASE WHEN complete = 1 AND quote_token IS NULL THEN 1 ELSE 0 END) AS unknown_quote,
  COUNT(*) AS total_positions
FROM wallet_token_pnl;

DROP VIEW IF EXISTS token_stats;
CREATE VIEW token_stats AS
SELECT k.token, k.symbol, k.name, k.creator, k.quote_token, k.ts AS launched_ts,
  COUNT(t.tx) AS trades, COUNT(DISTINCT t.trader) AS traders,
  COALESCE(SUM(t.quote_amt), 0) AS volume_quote, p.price AS last_price
FROM tokens k
LEFT JOIN trades t ON t.token = k.token
LEFT JOIN token_last_price p ON p.token = k.token
GROUP BY k.token;

DROP VIEW IF EXISTS creator_stats;
CREATE VIEW creator_stats AS
SELECT k.creator, COUNT(DISTINCT k.token) AS tokens_launched,
  COUNT(t.tx) AS trades, COUNT(DISTINCT t.trader) AS unique_traders
FROM tokens k LEFT JOIN trades t ON t.token = k.token
GROUP BY k.creator;
"""


def _f(x):
    return None if x is None else x / cfg.WEI


def _fd(x, decimals):
    """Float copy in whole units using the token's own decimals (M12).
    Defaults to 18 for existing rows / unknown tokens."""
    return None if x is None else x / (10 ** decimals)


def _decimals(v):
    try:
        d = int(v)
    except (TypeError, ValueError):
        return 18
    return d if 0 <= d <= 36 else 18


def _s(x):
    return None if x is None else str(x)


class DB:
    def __init__(self, path=None):
        path = path or cfg.DB_PATH
        folder = os.path.dirname(path)
        if path != ":memory:" and folder:
            os.makedirs(folder, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")  # lets the dashboard read while the indexer writes
        self.conn.executescript(SCHEMA)
        # Migration for databases created before the decimals column existed.
        # CREATE TABLE IF NOT EXISTS does not add columns to an existing table.
        try:
            self.conn.execute("ALTER TABLE tokens ADD COLUMN decimals INTEGER NOT NULL DEFAULT 18")
            self.conn.commit()
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

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

    def delete_blocks(self, lo, hi):
        """Delete trades and launches in a block range (M2 reorg handling).
        Re-ingesting that range afterwards restores it without leaving rows
        from a reorged transaction with a different hash behind."""
        with self.conn:
            self.conn.execute("DELETE FROM trades WHERE block BETWEEN ? AND ?", (lo, hi))
            self.conn.execute("DELETE FROM tokens WHERE block BETWEEN ? AND ?", (lo, hi))

    # writes ----------------------------------------------------------
    def save(self, launches, trades, ts):
        # Decimals for tokens in this batch, plus already-known tokens, so
        # trade float amounts use each token's own decimals (M12). Unknown
        # tokens and native ETH default to 18.
        dec = {}
        quote = {}
        for l in launches:
            dec[l["token"]] = _decimals(l.get("decimals"))
            quote[l["token"]] = l["quote_token"]
        if trades:
            tokens_needed = {t["token"] for t in trades}
            if tokens_needed:
                q = ",".join("?" * len(tokens_needed))
                for r in self.conn.execute(
                        f"SELECT token, quote_token, decimals FROM tokens WHERE token IN ({q})",
                        list(tokens_needed)):
                    dec.setdefault(r["token"], _decimals(r["decimals"]))
                    quote.setdefault(r["token"], r["quote_token"])
        with self.conn:
            for l in launches:
                self.conn.execute(
                    "INSERT OR REPLACE INTO tokens VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (l["token"], l["curve"], l["creator"], l["name"], l["symbol"], l["uri"],
                     l["quote_token"], l["launch_id"], l["block"], ts.get(l["block"]),
                     l["tx"], l["log_index"], json.dumps({k: str(v) for k, v in l["extra"].items()}),
                     _decimals(l.get("decimals"))))
            for t in trades:
                qt = quote.get(t["token"])
                qdec = 18 if qt is None or qt == cfg.ZERO else dec.get(qt, 18)
                self.conn.execute(
                    "INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t["tx"], t["log_index"], t["block"], ts.get(t["block"]), t["side"],
                     t["trader"], t["token"], t["curve"], _s(t["token_amount"]), _s(t["token_fee"]),
                     _s(t["quote_amount"]), _s(t["quote_fee"]), _s(t["reserve_after"]),
                     _s(t["native_in"]), _fd(t["token_amount"], dec.get(t["token"], 18)),
                     _fd(t["quote_amount"], qdec)))

    def q(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params)]
