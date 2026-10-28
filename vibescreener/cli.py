import argparse
import sys

import requests

from . import config as cfg
from . import report
from .db import DB
from .decode import decode_curve, decode_launch, decode_router, pair_trades
from .explorer import Explorer, ExplorerError, backfill_launches
from .indexer import Indexer
from .rpc import Rpc, RpcAuthError
from . import brief, credits, meta, web


def factory_start_block():
    """Block in which the factory was deployed, via the Blockscout API."""
    base = cfg.EXPLORER + "/api/v2"
    addr = requests.get(f"{base}/addresses/{cfg.FACTORY}", timeout=30).json()
    tx = requests.get(f"{base}/transactions/{addr['creation_transaction_hash']}", timeout=30).json()
    return int(tx["block_number"])


def cmd_tx(rpc, db, args):
    logs = rpc.get_receipt_logs(args.hash.lower())
    idx = Indexer(rpc, db)
    for lg in logs:
        t0 = lg.topics[0] if lg.topics else ""
        if lg.address == cfg.FACTORY and t0 == cfg.TOPIC_LAUNCH:
            print("LAUNCH", decode_launch(lg))
        elif lg.address == cfg.ROUTER and t0 == cfg.TOPIC_ROUTER:
            print("ROUTER", decode_router(lg))
        elif t0 in (cfg.TOPIC_CURVE_BUY, cfg.TOPIC_CURVE_SELL):
            print("CURVE ", decode_curve(lg))
    routers = [decode_router(l) for l in logs if l.address == cfg.ROUTER and l.topics and l.topics[0] == cfg.TOPIC_ROUTER]
    curves = [c for c in (decode_curve(l) for l in logs if l.topics and l.topics[0] in (cfg.TOPIC_CURVE_BUY, cfg.TOPIC_CURVE_SELL)) if c]
    trades, unpaired, skipped = pair_trades(routers, curves)
    for t in trades:
        print("TRADE ", t)
    print(f"unpaired={unpaired} skipped_kinds={dict(skipped)}")
    if args.save:
        print(idx.ingest(logs))


def run_service(rpc, db, poll=3.0, explorer=None, log=print, serve_web=False, db_path=None):
    """What a hosted service runs: fill in token launches once, then follow the chain.
    A failing explorer must not stop indexing, so launches are retried on the next start."""
    if cfg.on_railway_without_storage():
        log("WARNING: no volume attached and VIBESCREENER_DB is not set, so the database lives in "
            "the container and is wiped on every redeploy. Add a volume (see DEPLOY.md).")
    log(f"database: {db_path or cfg.DB_PATH}")
    if serve_web:
        web.start_background(db_path or cfg.DB_PATH, cfg.web_port(), log=log)
        meta.start_background(db_path or cfg.DB_PATH, log=log)
        brief.start_background(db_path or cfg.DB_PATH, log=log)
        credits.start_background(db_path or cfg.DB_PATH, log=log)
    log("rpc: " + ("alchemy" if "alchemy" in cfg.RPC_URL else "custom/public") + " endpoint")
    try:
        rpc.block_number()
    except RpcAuthError:
        if getattr(rpc, "url", cfg.PUBLIC_RPC) != cfg.PUBLIC_RPC:
            log("ERROR: the RPC provider rejected the API key (401). Check ALCHEMY_API_KEY: it must be "
                "an active key for an app on Robinhood Chain Testnet, with no spaces. "
                "Using the public RPC until that is fixed; it is slower and rate-limited.")
            rpc.url = cfg.PUBLIC_RPC
    except Exception:
        pass  # a temporary network problem; follow() retries on its own
    try:
        res = backfill_launches(explorer or Explorer(), db, log=log)
        if res["pages"]:
            log(f"launch backfill finished: {res['launches']} launches")
    except (ExplorerError, requests.RequestException) as e:
        log(f"launch backfill paused ({e}); it resumes on the next start")
    Indexer(rpc, db, log=log).follow(poll=poll)


def main(argv=None):
    p = argparse.ArgumentParser(prog="vibescreener", description="vibe/vibe testnet indexer")
    p.add_argument("--db", default=cfg.DB_PATH)
    p.add_argument("--rpc", default=cfg.RPC_URL)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backfill", help="index a block range")
    b.add_argument("--from-block", default="auto", help="block number, or 'auto' = factory deploy block")
    b.add_argument("--to-block", default="latest")
    b.add_argument("--chunk", type=int, default=5000, help="initial blocks per query (auto-adjusts)")

    f = sub.add_parser("follow", help="keep indexing new blocks")
    f.add_argument("--poll", type=float, default=cfg.poll_seconds())

    l = sub.add_parser("launches", help="backfill every token launch from the explorer (resumable)")
    l.add_argument("--max-pages", type=int, default=None, help="stop after N pages (for a quick test)")
    l.add_argument("--restart", action="store_true", help="forget progress and crawl again")

    n = sub.add_parser("run", help="deployment entrypoint: backfill launches once, then follow forever")
    n.add_argument("--poll", type=float, default=cfg.poll_seconds())

    t = sub.add_parser("tx", help="decode one transaction (use to verify the decoder)")
    t.add_argument("hash")
    t.add_argument("--save", action="store_true")

    r = sub.add_parser("report", help="print leaderboards")
    r.add_argument("--top", type=int, default=15)
    r.add_argument("--hours", type=int, default=24)

    w = sub.add_parser("serve", help="dashboard + JSON API only (reads an existing database)")
    w.add_argument("--port", type=int, default=cfg.web_port())

    sub.add_parser("quotes", help="list quote tokens seen (tells you which are ETH-like)")
    sub.add_parser("start-block", help="print the factory deploy block")
    e = sub.add_parser("export", help="write leaderboards to JSON")
    e.add_argument("--out", default="vibescreener.json")

    args = p.parse_args(argv)
    if args.cmd == "start-block":
        print(factory_start_block())
        return 0

    if args.cmd == "serve":
        DB(args.db)  # make sure the file and views exist
        print(f"dashboard on http://localhost:{args.port}  (database: {args.db})")
        web.serve(args.db, args.port).serve_forever()
        return 0

    db, rpc = DB(args.db), Rpc(args.rpc)
    if args.cmd == "backfill":
        start = factory_start_block() if args.from_block == "auto" else int(args.from_block)
        end = rpc.block_number() if args.to_block == "latest" else int(args.to_block)
        print(Indexer(rpc, db).backfill(start, end, chunk=args.chunk))
    elif args.cmd == "follow":
        Indexer(rpc, db).follow(poll=args.poll)
    elif args.cmd == "launches":
        print(backfill_launches(Explorer(), db, max_pages=args.max_pages, restart=args.restart))
    elif args.cmd == "run":
        run_service(rpc, db, poll=args.poll, serve_web=True, db_path=args.db)
    elif args.cmd == "tx":
        cmd_tx(rpc, db, args)
    elif args.cmd == "report":
        report.print_report(db, args.top, args.hours)
    elif args.cmd == "quotes":
        for row in report.quotes(db):
            print(row["quote_token"], report.qlabel(row["quote_token"]), row["tokens"])
    elif args.cmd == "export":
        print(report.export_json(db, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
