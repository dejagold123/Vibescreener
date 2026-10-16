"""Leaderboards printed to the terminal, and a JSON export for a dashboard."""
import json
import time

from . import config as cfg


def short(a):
    return a if not a else a[:6] + "…" + a[-4:]


def qlabel(q):
    if q is None:
        return "unknown-quote"
    if q == cfg.ZERO:
        return "ETH"
    if q == cfg.VIBEVIBE_TOKEN:
        return "VIBEVIBE"
    return short(q)


def leaderboard_wallets(db, top=15):
    return db.q("SELECT * FROM wallet_pnl WHERE trades > 0 ORDER BY pnl_quote DESC LIMIT ?", (top,))


def trending_tokens(db, hours=24, top=15):
    since = int(time.time()) - hours * 3600
    return db.q(
        """SELECT k.symbol, t.token AS token, k.quote_token, COUNT(*) AS trades,
                  COUNT(DISTINCT t.trader) AS traders, SUM(t.quote_amt) AS volume_quote
           FROM trades t LEFT JOIN tokens k ON k.token = t.token
           WHERE t.ts >= ? GROUP BY t.token ORDER BY traders DESC, trades DESC LIMIT ?""",
        (since, top))


def top_creators(db, top=15):
    return db.q("SELECT * FROM creator_stats ORDER BY unique_traders DESC, trades DESC LIMIT ?", (top,))


def quotes(db):
    return db.q("""SELECT k.quote_token, COUNT(*) AS tokens FROM tokens k GROUP BY k.quote_token
                   UNION ALL SELECT NULL, COUNT(DISTINCT t.token) FROM trades t
                   WHERE t.token NOT IN (SELECT token FROM tokens)""")


def print_report(db, top=15, hours=24):
    n_tok = db.q("SELECT COUNT(*) n FROM tokens")[0]["n"]
    n_tr = db.q("SELECT COUNT(*) n FROM trades")[0]["n"]
    print(f"\nVibeScreener — {n_tok} tokens, {n_tr} trades indexed\n")
    print("TOP WALLETS BY ESTIMATED PnL (per quote token; units are whole quote tokens)")
    for r in leaderboard_wallets(db, top):
        print(f"  {short(r['trader'])}  {qlabel(r['quote_token']):>13}  pnl {r['pnl_quote']:>14.6f}  "
              f"spent {r['quote_spent']:>12.6f}  trades {r['trades']:>4}  tokens {r['tokens_traded']}")
    print(f"\nTRENDING TOKENS (last {hours}h, by unique traders)")
    for r in trending_tokens(db, hours, top):
        print(f"  {(r['symbol'] or short(r['token'])):>10}  traders {r['traders']:>4}  trades {r['trades']:>4}  "
              f"volume {r['volume_quote']:>12.6f} {qlabel(r['quote_token'])}")
    print("\nTOP CREATORS (by unique traders across their tokens)")
    for r in top_creators(db, top):
        print(f"  {short(r['creator'])}  launched {r['tokens_launched']:>3}  "
              f"traders {r['unique_traders']:>4}  trades {r['trades']:>5}")


def export_json(db, path, top=100):
    out = {
        "generated_at": int(time.time()),
        "wallets": leaderboard_wallets(db, top),
        "tokens": db.q("SELECT * FROM token_stats ORDER BY traders DESC LIMIT ?", (top,)),
        "creators": top_creators(db, top),
    }
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    return path
