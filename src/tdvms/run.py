"""The campaign, supervised. One process that submits, fetches and refills.

    tdvms run --address you@gmail.com --slots 8
    tdvms run --once --dry-run --address you@gmail.com --slots 8
    tdvms run --exit-when-done --address you@gmail.com --slots 8

This is the command the whole project is for. It owns the ledger, the mailbox
and the portal client at once, so submit, fetch and free-the-slot happen inside
one function under one lock. The tool it replaces split those across three
programs and a subprocess call, and every stall the campaign produced -- a slot
held against a request nobody made, a chunk retired with its slot never
refilled, a queue that drained to zero unnoticed -- lived in the seams between
them.

Nothing here needs an operator between ticks. `status` remains for looking, and
`plan` for adding work. By default the loop never exits on its own, because new
windows can be planned into a running campaign; `--exit-when-done` stops it once
every chunk has an outcome, which is what lets campaigns run one after another
(`tdvms queue` does exactly that, in one process).
"""
import argparse
import signal
import sys
import time

from tdvms import campaign, notify
from tdvms.client import Client
from tdvms.ledger import Ledger
from tdvms.mailbox import Mailbox
from tdvms.report import summarise
from tdvms.slots import Pool, PoolHeld, hold
from tdvms.supervisor import Config, cycle

NAME = "run"
HELP = "the supervised loop: reap, reclaim, retire, submit"


def add_args(p, single=True):
    """`run`'s options. `single=False` leaves out the per-campaign ones for `queue`."""
    p.add_argument("--address", required=True,
                   help="the BASE address, e.g. you@gmail.com. The pool extends "
                        "it to you+a1@..., you+a2@... -- the portal keys its "
                        "one-request-at-a-time limit on the literal string, so "
                        "each is a separate slot delivering to one inbox.")
    p.add_argument("--slots", type=int, default=8,
                   help="how many plus-addresses the pool holds")
    if single:
        p.add_argument("--station", action="append", default=None, dest="stations",
                       help="repeatable; restrict submissions to these stations. "
                            "Without it the loop round-robins so one station "
                            "cannot starve the rest.")
        p.add_argument("--out-dir", default=None,
                       help="where archives are filed. Default: the campaign's "
                            "own folder -- campaigns/<name>/raw, <name>_raw beside "
                            "<name>_ledger.jsonl, or afad_raw")
        p.add_argument("--once", action="store_true", help="one cycle, then exit")
        p.add_argument("--exit-when-done", action="store_true",
                       help="exit once every chunk has an outcome, instead of "
                            "waiting for more to be planned")
        p.add_argument("--dry-run", action="store_true",
                       help="report the cycle without touching mail, ledger or portal")
    p.add_argument("--interval", type=int, default=60,
                   help="seconds between cycles; ignored with --once")

    g = p.add_argument_group("disk")
    g.add_argument("--min-free-gb", type=float, default=None,
                   help="free space to keep under the download folder; below it "
                        "nothing is downloaded or submitted. Default: twice the "
                        "largest archive so far, at least 2 GB. 0 turns it off.")

    g = p.add_argument_group("notifications")
    g.add_argument("--notify", action="append", default=None, metavar="TARGET",
                   help="repeatable: an ntfy topic URL (https://ntfy.sh/<topic>), "
                        "any URL that accepts a POST, or 'desktop'. Default: "
                        "TDVMS_NOTIFY, else what tdvms-tui saved.")

    g = p.add_argument_group("mailbox")
    g.add_argument("--folder", default="INBOX")
    g.add_argument("--search", default="(UNSEEN)",
                   help="raw IMAP criteria. The default scans everything unread, "
                        "which is right for a dedicated mailbox and wrong for one "
                        'carrying other mail -- narrow it, e.g. \'(UNSEEN SUBJECT "TDVMS")\'')
    g.add_argument("--claim-unknown", action="store_true",
                   help="act on links addressed to slots this ledger never "
                        "submitted from. Off by default: several campaigns can "
                        "share one mailbox and each must leave the others' mail "
                        "alone, or the owner waits forever for a burnt link.")

    g = p.add_argument_group("self-repair")
    g.add_argument("--claim-timeout", type=int, default=600,
                   help="seconds before a `claimed` row is assumed to have died "
                        "mid-POST and is requeued")
    g.add_argument("--submit-timeout", type=int, default=21600,
                   help="seconds before a `submitted` row is assumed lost. "
                        "Default 6 h, about 4x the median turnaround.")
    g.add_argument("--max-attempts", type=int, default=3,
                   help="after this many, a chunk is retired rather than requeued")
    g.add_argument("--busy-cooldown", type=int, default=1800,
                   help="seconds a slot waits after the portal answers 111. A "
                        "local `failed` does not free the portal's queue.")
    g.add_argument("--nodata-streak", type=int, default=8,
                   help="no-data answers, with nothing ever fetched, before a "
                        "station is retired. One station burned ~30 slots "
                        "establishing the same fact 26 times.")
    g.add_argument("--no-retire", dest="retire", action="store_false",
                   help="warn about a dead station but keep submitting for it")
    p.add_argument("--timeout", type=int, default=180,
                   help="seconds to wait for the portal to answer a submission")
    return p


def config(args, out_dir):
    """The loop's `Config` from parsed options, for one campaign's folder."""
    return Config(out_dir=str(out_dir), claim_timeout=args.claim_timeout,
                  submit_timeout=args.submit_timeout, max_attempts=args.max_attempts,
                  nodata_streak=args.nodata_streak, busy_cooldown=args.busy_cooldown,
                  retire=args.retire, stations=getattr(args, "stations", None),
                  claim_unknown=args.claim_unknown,
                  dry_run=getattr(args, "dry_run", False), min_free_gb=args.min_free_gb)


def take_pool(address, ledger_path):
    """The pool lock, or an exit naming whoever has it."""
    try:
        return hold(address, ledger_path)
    except PoolHeld as e:
        sys.exit(f"[ERROR] {address} is already being worked ({e}).\n"
                 f"        Two runs on one address share its slots and its "
                 f"inbox, and each\n        downloads and marks read the "
                 f"other's mail. Stop that run first.")


def loop(ledger, pool, cfg, mailbox, client, interval, once=False, until_done=False,
         watch=None):
    """Cycles until stopped; with `until_done`, until every chunk has an outcome."""
    quiet, paused = 0, None
    while True:
        stamp = time.strftime("%H:%M:%S")
        result = cycle(ledger, pool, cfg, mailbox, client,
                       log=lambda m: print(f"[{stamp}] {m}", flush=True))
        if result.paused != paused:
            # Said once when it starts and once when it ends, not every minute.
            print(f"[{stamp}] [DISK] " + (result.paused or "room again -- resuming"),
                  flush=True)
            paused = result.paused
        if result.quiet:
            quiet += 1
            # A loop that prints a paragraph every 60 seconds trains its
            # operator to stop reading it, and the one cycle that mattered goes
            # past unseen. Quiet cycles collapse to a single counted line.
            print(f"[{stamp}] quiet ({quiet} in a row), "
                  f"{len(pool.free())}/{len(pool)} slots free", flush=True)
        else:
            quiet = 0
            print(f"[{stamp}] {result.summary()}", flush=True)
        if watch is not None:
            watch.after(result)
        if once:
            return
        if until_done and ledger.complete():
            print(f"[{stamp}] every chunk has an outcome -- done", flush=True)
            return
        time.sleep(interval)


def run(args):
    ledger = Ledger(args.ledger)
    if not ledger.rows():
        sys.exit(f"[ERROR] {ledger.path} is empty. Run `tdvms plan` first, or "
                 f"`tdvms adopt --from <old ledger>`.")
    camp = campaign.for_ledger(args.ledger)
    out_dir = args.out_dir or camp.raw
    pool = Pool(args.address, args.slots)
    # A dry run flags no mail and submits nothing, so it may look at a pool a
    # live run is working. Anything else must have the pool to itself.
    lock = None if args.dry_run else take_pool(args.address, ledger.path)  # noqa: F841
    cfg = config(args, out_dir)
    mailbox = Mailbox(args.folder, args.search)
    client = Client(timeout=args.timeout)
    notifier = notify.Notifier(notify.targets(args.notify))
    watch = None if args.dry_run else notify.Watch(notifier, camp.name, ledger, pool)

    if args.dry_run:
        print("dry run — no mail flagged, no ledger writes, no submissions")
    print(f"{len(pool)} slot(s) from {args.address}; ledger {ledger.path}; "
          f"archives to {out_dir}"
          + (f"; notifying {', '.join(notifier.targets)}" if notifier else ""))
    loop(ledger, pool, cfg, mailbox, client, args.interval, once=args.once,
         until_done=args.exit_when_done, watch=watch)
    if args.once:
        print()
        summarise(ledger, pool)
    return 0


def handle_signals():
    """SIGINT and SIGTERM both stop the loop the same clean way.

    SIGINT is restored explicitly: a job started with `&` from a script inherits
    it IGNORED, so the queue's `kill -INT` did nothing and a finished run lived
    on, reading mail that belonged to the next one.
    """
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, _interrupt)


STOPPED = ("\nstopped. Nothing is lost -- the ledger is the state, and every "
           "in-flight chunk is reclaimed by the next `run`.")


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def main():
    p = argparse.ArgumentParser(prog="tdvms run", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ledger", default="tdvms_ledger.jsonl")
    add_args(p)
    handle_signals()
    try:
        return run(p.parse_args())
    except KeyboardInterrupt:
        print(STOPPED)
        return 0


if __name__ == "__main__":
    sys.exit(main())
