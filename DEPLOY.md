# Deploying VibeScreener on Railway

The repo has a `Dockerfile`, so Railway builds it automatically. The container runs
`python -m vibescreener run`, which fills in every past token launch once and then follows
the chain forever. All settings are environment variables, so you add them in Railway's
**Variables** tab and never put a key in code or in the repo.

## 1. Rotate your Alchemy key first

An earlier version of `REPORT.md` contained a real key in a public repo. In the Alchemy
dashboard, delete that key and create a new one for your Robinhood Chain Testnet app.
Use only the new key below.

## 2. Create the service

1. Railway → **New Project** → **Deploy from GitHub repo** → pick `dejagold123/Vibescreener`.
2. Railway detects the `Dockerfile` and builds it. The first deploy will run without your
   settings and use the public RPC; that's fine, you add the variables next.

## 3. Add the variables

Open the service → **Variables** tab → add:

| Variable | Value | Needed? |
|---|---|---|
| `ALCHEMY_API_KEY` | just the key, e.g. `abc123...` (not the whole URL) | recommended |
| `VIBESCREENER_RPC` | a full RPC URL; overrides `ALCHEMY_API_KEY` | optional |
| `VIBESCREENER_DB` | database path; overrides the volume path | optional |
| `VIBESCREENER_POLL` | seconds between polls (default `3`) | optional |

Railway redeploys when you save variables. You can use any RPC provider: set
`VIBESCREENER_RPC` to its URL instead of `ALCHEMY_API_KEY`.

## 4. Add a volume (this is what keeps your data)

Without a volume the database lives inside the container and is **wiped on every
redeploy or restart**. The follower would then restart from the chain tip and lose all history.

1. On the project canvas press `Cmd/Ctrl + K` (or right-click the canvas) → **Volume**.
2. Attach it to the VibeScreener service and set the mount path to `/data`.

Railway sets `RAILWAY_VOLUME_MOUNT_PATH` itself when a volume is attached, and
VibeScreener stores `vibescreener.db` there automatically. You don't add a variable for it.

## 5. Check the logs

A healthy start looks like this:

```
database: /data/vibescreener.db
rpc: alchemy endpoint
launch backfill: 20 pages, N launches stored
...
launch backfill finished: N launches
following from block 136700000
blocks 136700010-136700018: +0 launches, +2 trades (total 0/2)
```

- `WARNING: no volume attached...` means step 4 is missing.
- `launch backfill paused (...)` means the explorer was unreachable. Indexing continues,
  and the launch backfill resumes where it stopped on the next restart.
- `node limit: using chunks of N blocks` is normal. The node's per-request block limit was
  detected once and is remembered.
- `rpc error, retrying in 10s` is a temporary network problem; the service retries by itself.
  API keys are always masked in logs as `/v2/***`.

## 6. Open the dashboard

The service also serves the dashboard and a JSON API on the port Railway provides (`PORT`).
To get a public address: open the service → **Settings** → **Networking** → **Generate Domain**.
Open that link to see the dashboard. The JSON API lives under `/api/` (for example `/api/overview`,
`/api/trending`, `/api/wallets`, `/api/creators`, `/api/feed`, `/api/token/<address>`,
`/api/wallet/<address>`). `/health` returns `ok`. It is read-only; nothing on it can change data.

## Things to know

- **RPC usage.** The chain makes roughly 8-10 blocks per second. On Alchemy's free plan,
  which allows only 10 blocks per request, that's about one `eth_getLogs` call per second
  (an estimate: roughly 70,000-85,000 a day). Watch your usage in the Alchemy dashboard. If you hit the quota, point
  `VIBESCREENER_RPC` at another provider; the service finds that node's block limit itself.
  Raising `VIBESCREENER_POLL` does not reduce calls much, it only batches them.
- **History.** Token launches are backfilled from the explorer. Trades are indexed only
  from the moment the service starts, so wallet PnL only counts positions whose buys were
  seen. Positions with missing history are left out and the report says how many.
- **Seeing results.** Use the dashboard (step 6). The terminal `report` command still works
  wherever the database file can be read.
- **Public once you generate a domain.** The dashboard has no login. It shows only
  on-chain data, but anyone with the link can open it.
