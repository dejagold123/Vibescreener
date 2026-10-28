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
python -m vibescreener launches                # every past token launch (names, quote tokens) via the explorer; resumable
python -m vibescreener backfill                # trades from the factory deploy block (looked up via the explorer)
python -m vibescreener follow
python -m vibescreener run                     # what a server runs: launches once, then follow forever

# 3. look at the data
python -m vibescreener report
python -m vibescreener quotes                  # which quote tokens exist; shows which are ETH-like
python -m vibescreener export --out vibescreener.json   # leaderboards as JSON

# 4. dashboard + JSON API (also started automatically by `run`)
python -m vibescreener serve --port 8000       # http://localhost:8000
```

Options: `--rpc <url>` to use your own node, `--db <file>`.
`backfill --from-block N` skips the explorer lookup. The chunk size adapts to what the node allows.

## Settings (environment variables)

| Variable | Meaning |
|---|---|
| `ALCHEMY_API_KEY` | just your key; the Alchemy RPC URL is built from it |
| `VIBESCREENER_RPC` | a full RPC URL (overrides `ALCHEMY_API_KEY`) |
| `VIBESCREENER_DB` | database file path (default: the Railway volume if attached, else `./vibescreener.db`) |
| `VIBESCREENER_POLL` | seconds between polls in `follow` / `run` (default 3) |
| `IPFS_GATEWAY` | optional preferred IPFS gateway for token images and descriptions |
| `VIBESCREENER_META` | set to `0` to turn token metadata fetching off |
| `PORT` / `VIBESCREENER_PORT` | dashboard port (Railway sets `PORT`; default 8000) |
| `LLM_API_KEY` (or `OPENROUTER_API_KEY`) | key for the AI market brief and Ask box. Without it both still work using plain data summaries |
| `LLM_BASE_URL` | any OpenAI-compatible endpoint (default `https://openrouter.ai/api/v1`) |
| `LLM_MODEL` | comma list tried in order (default: two free OpenRouter models) |
| `BRIEF_EVERY_HOURS` | how often the market brief is regenerated (default 3) |
| `ASK_FREE_PER_DAY` / `ASK_GLOBAL_PER_DAY` | free questions per visitor (default 5) and for the whole site (default 40) per day |
| `TOKEN_ADDRESS` + `TREASURY_ADDRESS` | turn on paying for extra questions with your token; both must be set |
| `ASK_PRICE` | token cost of one paid question (default 1) |

Set keys in the Railway Variables tab, never in chat or in the repo.

**AI and credits, briefly.** The AI never sees the database: the server looks up the figures, the model only phrases them, and any answer containing a number that was not in those figures is thrown away for a plain summary. Credits: visitors sign in by signing a free message with their wallet, send your token to `TREASURY_ADDRESS`, and the server credits the sender after reading the transfer from the explorer. Free questions are used first. The only thing the web server writes is counters, sessions and balances; indexed chain data cannot be changed from the web.

Never commit keys to the repo. Deploying: see [DEPLOY.md](DEPLOY.md).

## What it reads

| Contract | Address | Event used |
|---|---|---|
| Factory | `0xe794…fea0` | one launch event per token (token, curve, creator, name, symbol, metadata URI, quote token) |
| Router | `0x2784…eb1` | one event per trade (kind, trader, curve, amounts) |
| Per-token curve | many | buy / sell events (tokens received, fees, reserve) |

Each router trade is joined to the curve event that precedes it in the same transaction.
Layouts are documented at the top of `vibescreener/decode.py`.

## Dashboard pages

- **Tokens** (home): a DexScreener-style table with price, age, transactions, a buy/sell bar, volume, makers and price
  change over 5m, 1h, 6h and 24h. Tabs for Trending, New, Gainers and Top volume; filters for ETH, VIBEVIBE or all
  quote tokens and a ranking window; click a column to sort. A live trades rail sits beside it.
- **Token page**: candlestick chart (1m, 5m, 15m, 1h) with volume, 24h buy/sell activity, trades, top traders,
  image, description and links.
- **Wallets**: leaderboard by estimated PnL and win rate, per quote token. **Wallet page**: positions, best and worst.
- **Creators**: how each creator's tokens fared. **Search**: token name, symbol or address, and wallet address.

Volume is in each token's own quote token and is only ranked inside one quote token. Price changes compare trade prices
(buys only when there are two or more) and ignore fees. Token images and descriptions are fetched slowly in the background
from each launch's IPFS link, newest tokens first.

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
- Trades are only indexed from where you start. PnL counts only positions whose buys were seen and
  whose quote token is known; the report prints how many positions it left out. Run `launches` so every
  token's quote token is known.
