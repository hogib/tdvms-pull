"""Several campaigns, one after another, on one address pool.

    tdvms queue balikesir silivri --address you@gmail.com --slots 15
    tdvms queue campaigns/van-2011/ledger.jsonl elba.jsonl --address you@gmail.com

Campaigns on one address cannot run side by side -- they would share its slots
and read each other's mail -- so they run in turn. This used to be a shell
script polling each ledger every five minutes and stopping a finished `run`
with SIGINT, which a backgrounded job ignores: the finished run lived on and
read the next campaign's mail for three hours. Here the queue is one process
that holds the address the whole time and moves on the moment a campaign has an
outcome for every chunk, keeping each slot's BUSY cooldown across the change.

A campaign is named as `tdvms-tui` lists it, or by its ledger path. Each one
downloads into its own folder (see `campaign`). A finished campaign is skipped,
so re-running the same queue after a stop picks up where it left off.
"""
import argparse
import sys

from tdvms import campaign, notify
from tdvms import run as runner
from tdvms.client import Client
from tdvms.ledger import IN_FLIGHT
from tdvms.mailbox import Mailbox
from tdvms.slots import Pool, relabel

NAME = "queue"
HELP = "run several campaigns one after another on one address"


def add_args(p):
    p.add_argument("campaigns", nargs="+", metavar="CAMPAIGN",
                   help="campaign names (as tdvms-tui lists them) or ledger paths, "
                        "in the order to run them")
    p.add_argument("--root", default=".",
                   help="where campaign names are looked up (default: here)")
    runner.add_args(p, single=False)
    return p


def run(args):
    try:
        camps = [campaign.find(spec, args.root) for spec in args.campaigns]
    except LookupError as e:
        sys.exit(f"[ERROR] {e}")
    pool = Pool(args.address, args.slots)
    lock = runner.take_pool(args.address, camps[0].path)
    mailbox = Mailbox(args.folder, args.search)
    client = Client(timeout=args.timeout)
    notifier = notify.Notifier(notify.targets(args.notify))
    print(f"queue: {' -> '.join(c.name for c in camps)}; {len(pool)} slot(s) from "
          f"{args.address}" + (f"; notifying {', '.join(notifier.targets)}"
                               if notifier else ""), flush=True)
    for c in camps:
        rows = c.ledger.rows()
        if not rows:
            print(f"== {c.name}: {c.path} is empty, skipping", flush=True)
            continue
        if c.ledger.complete():
            print(f"== {c.name}: already complete, skipping", flush=True)
            continue
        relabel(lock, c.path)
        pending = sum(1 for r in rows if r["state"] == "pending")
        inflight = sum(1 for r in rows if r["state"] in IN_FLIGHT)
        print(f"== {c.name}: {pending} pending, {inflight} in flight; archives to "
              f"{c.raw}", flush=True)
        runner.loop(c.ledger, pool, runner.config(args, c.raw), mailbox, client,
                    args.interval, until_done=True,
                    watch=notify.Watch(notifier, c.name, c.ledger, pool))
    print("queue finished", flush=True)
    notifier.send("tdvms: queue finished",
                  f"{', '.join(c.name for c in camps)}: every chunk has an outcome.")
    return 0


def main():
    p = argparse.ArgumentParser(prog="tdvms queue", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    add_args(p)
    runner.handle_signals()
    try:
        return run(p.parse_args())
    except KeyboardInterrupt:
        print(runner.STOPPED)
        return 0


if __name__ == "__main__":
    sys.exit(main())
