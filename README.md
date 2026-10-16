# VibeScreener indexer (v1, on-chain only)

Indexes vibe/vibe on Robinhood Chain Testnet (chain id 46630) into SQLite:
token launches, every buy and sell, and wallet / token / creator analytics.

## Run it

```bash
pip install -r requirements.txt
python -m unittest discover -s tests          # decoder checks against 3 real transactions

# 1. verify the decoder on your own live transactions
python -m vibescreener tx 0xd937397adb0922bf0803160fa1907d54fffff4645c26e3454f4307c49717a978

# 2. index history, then keep following the chain
python -m vibescreener backfill                # from the factory deploy block (looked up via the explorer)
python -m vibescreener follow

# 3. look at the data
python -m vibescreener report
python -m vibescreener quotes                  # which quote tokens exist; shows which are ETH-like
python -m vibescreener export --out vibescreener.json   # feed for the dashboard
```

Options: `--rpc <url>` (or env `VIBESCREENER_RPC`) to use your Alchemy endpoint, `--db <file>`.
`backfill --from-block N` skips the explorer lookup. The chunk size adapts to what the node allows.

## What it reads

| Contract | Address | Event used |
|---|---|---|
| Factory | `0xe794…fea0` | one launch event per token (token, curve, creator, name, symbol, metadata URI, quote token) |
| Router | `0x2784…eb1` | one event per trade (kind, trader, curve, amounts) |
| Per-token curve | many | buy / sell events (tokens received, fees, reserve) |

Each router trade is joined to the curve event that precedes it in the same transaction.
Layouts are documented at the top of `vibescreener/decode.py`.

## Tables and views

`tokens`, `trades` (exact wei amounts as text, float copies for maths), and views
`token_last_price`, `token_stats`, `creator_stats`, `wallet_token_pnl`, `wallet_pnl`.

## Known limits

- The contracts are unverified. Layouts were inferred from one launch, one sell and two buys,
  and cross-checked against ERC-20 transfers. Two fields per event are unidentified (kept in `tokens.extra`).
- Sells of tokens quoted in VIBEVIBE have not been seen yet. Run `tx` on one to confirm
  the quote amount is VIBEVIBE and not ETH.
- PnL is per quote token (ETH vs VIBEVIBE are never added together). Token transfers between
  wallets are not tracked, and prices ignore fees, so treat PnL as an estimate.
- Launches before the factory block you backfill from are missing, so their tokens show an unknown quote token.
