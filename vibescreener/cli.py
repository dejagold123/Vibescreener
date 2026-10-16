import argparse
import sys

import requests

from . import config as cfg
from . import report
from .db import DB
from .decode import decode_curve, decode_launch, decode_router, pair_trades
from .indexer import Indexer
from .rpc import Rpc


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
    f.add_argument("--poll", type=float, default=3.0)

    t = sub.add_parser("tx", help="decode one transaction (use to verify the decoder)")
    t.add_argument("hash")
    t.add_argument("--save", action="store_true")

    r = sub.add_parser("report", help="print leaderboards")
    r.add_argument("--top", type=int, default=15)
    r.add_argument("--hours", type=int, default=24)

    sub.add_parser("quotes", help="list quote tokens seen (tells you which are ETH-like)")
    sub.add_parser("start-block", help="print the factory deploy block")
    e = sub.add_parser("export", help="write leaderboards to JSON")
    e.add_argument("--out", default="vibescreener.json")

    args = p.parse_args(argv)
    if args.cmd == "start-block":
        print(factory_start_block())
        return 0

    db, rpc = DB(args.db), Rpc(args.rpc)
    if args.cmd == "backfill":
        start = factory_start_block() if args.from_block == "auto" else int(args.from_block)
        end = rpc.block_number() if args.to_block == "latest" else int(args.to_block)
        print(Indexer(rpc, db).backfill(start, end, chunk=args.chunk))
    elif args.cmd == "follow":
        Indexer(rpc, db).follow(poll=args.poll)
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
