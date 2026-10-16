# How to run the VibeScreener indexer

A step-by-step guide for first-time users. You need a computer with a terminal.
Commands are for Mac/Linux; on Windows use `python` instead of `python3`.

## 1. Check Python and Git are installed

```
python3 --version
git --version
```

You need Python 3.10 or newer. If either is missing, install it from
[python.org](https://www.python.org/downloads/) or [git-scm.com](https://git-scm.com/).

## 2. Download the project

```
git clone https://github.com/dejagold123/Vibescreener.git
cd Vibescreener
```

## 3. Set up a private Python environment

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows, activate with `.venv\Scripts\activate`.
You should see `(.venv)` at the start of your prompt. Repeat the `activate`
line every time you open a new terminal.

## 4. Run the tests

```
python -m unittest discover -s tests
```

You should see `OK` at the end.

## 5. Check it against the live chain

```
python -m vibescreener tx 0xd937397adb0922bf0803160fa1907d54fffff4645c26e3454f4307c49717a978
```

This is a real sell transaction. You should see `ROUTER`, `CURVE` and `TRADE`
lines ending with `unpaired=0`. If it errors here, stop and report the error
before going further.

## 6. Index a small slice first

```
python -m vibescreener backfill --from-block 136197000
```

This indexes from just before the test transactions, so it finishes quickly and
prints progress. The data is saved to `vibescreener.db` in the same folder.

Don't use plain `backfill` yet. It starts at the factory's deploy block and can
take a long time.

## 7. Look at the results

```
python -m vibescreener report
python -m vibescreener quotes
```

- `report` shows top wallets, trending tokens, and top creators.
- `quotes` lists the quote tokens found, including BTX's quote token.

## 8. Keep it updating

```
python -m vibescreener follow
```

Leave this running in its own terminal. Open a second terminal (and activate
the environment again) to run `report`.

## If something goes wrong

| Problem | Fix |
|---|---|
| `ModuleNotFoundError: requests` | The environment isn't active. Repeat the `activate` line from step 3. |
| Slow, `429`, or timeout errors | The public RPC is rate-limiting you. Create a free Alchemy app on Robinhood Chain Testnet and add `--rpc https://robinhood-testnet.g.alchemy.com/v2/YOUR_KEY` before the command. Never commit that key to GitHub. |
| Stopped a backfill with Ctrl+C | Safe, but re-running starts again from the beginning. To continue, pass the last block it printed as `--from-block`. |
| `backfill` can't find the start block | Pass `--from-block <number>` yourself instead of relying on the automatic lookup. |

## Useful commands

| Command | What it does |
|---|---|
| `python -m vibescreener tx <hash>` | Decode one transaction (add `--save` to store it) |
| `python -m vibescreener backfill --from-block N` | Index from block N to the latest |
| `python -m vibescreener follow` | Keep indexing new blocks |
| `python -m vibescreener launches` | Fetch every past token launch from the explorer (resumable) |
| `python -m vibescreener run` | Launches once, then follow forever (what a server runs) |
| `python -m vibescreener report` | Print leaderboards |
| `python -m vibescreener quotes` | List quote tokens seen |
| `python -m vibescreener export --out vibescreener.json` | Write leaderboards to JSON for a dashboard |
| `python -m vibescreener start-block` | Print the factory deploy block |
