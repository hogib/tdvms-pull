"""The campaign, supervised. One process that submits, fetches and refills.

    tdvms run --address you@gmail.com --slots 8
    tdvms run --once --dry-run --address you@gmail.com --slots 8

This is the command the whole project is for. It owns the ledger, the mailbox
and the portal client at once, so submit, fetch and free-the-slot happen inside
one function under one lock. The tool it replaces split those across three
programs and a subprocess call, and every stall the campaign produced -- a slot
held against a request nobody made, a chunk retired with its slot never
refilled, a queue that drained to zero unnoticed -- lived in the seams between
them.

Nothing here needs an operator between ticks. `status` remains for looking, and
`plan` for adding work.
"""
import argparse
import sys
import time

from tdvms.client import Client
from tdvms.ledger import Ledger
from tdvms.mailbox import Mailbox
from tdvms.report import summarise
from tdvms.slots import Pool
from tdvms.supervisor import Config, cycle

NAME = "run"
HELP = "the supervised loop: reap, reclaim, retire, submit"


def add_args(p):
    p.add_argument("--address", required=True,
                   help="the BASE address, e.g. you@gmail.com. The pool extends "
                        "it to you+a1@..., you+a2@... -- the portal keys its "
                        "one-request-at-a-time limit on the literal string, so "
                        "each is a separate slot delivering to one inbox.")
    p.add_argument("--slots", type=int, default=8,
                   help="how many plus-addresses the pool holds")
    p.add_argument("--station", action="append", default=None, dest="stations",
                   help="repeatable; restrict submissions to these stations. "
                        "Without it the loop round-robins so one station cannot "
                        "starve the rest.")
    p.add_argument("--out-dir", default="afad_raw")
    p.add_argument("--interval", type=int, default=60,
                   help="seconds between cycles; ignored with --once")
    p.add_argument("--once", action="store_true", help="one cycle, then exit")
    p.add_argument("--dry-run", action="store_true",
                   help="report the cycle without touching mail, ledger or portal")

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


def run(args):
    ledger = Ledger(args.ledger)
    if not ledger.rows():
        sys.exit(f"[ERROR] {ledger.path} is empty. Run `tdvms plan` first, or "
                 f"`tdvms adopt --from <old ledger>`.")
    pool = Pool(args.address, args.slots)
    cfg = Config(out_dir=args.out_dir, claim_timeout=args.claim_timeout,
                 submit_timeout=args.submit_timeout, max_attempts=args.max_attempts,
                 nodata_streak=args.nodata_streak, busy_cooldown=args.busy_cooldown,
                 retire=args.retire, stations=args.stations,
                 claim_unknown=args.claim_unknown, dry_run=args.dry_run)
    mailbox = Mailbox(args.folder, args.search)
    client = Client(timeout=args.timeout)

    if args.dry_run:
        print("dry run — no mail flagged, no ledger writes, no submissions")
    print(f"{len(pool)} slot(s) from {args.address}; ledger {ledger.path}")

    quiet = 0
    while True:
        stamp = time.strftime("%H:%M:%S")
        result = cycle(ledger, pool, cfg, mailbox, client,
                       log=lambda m: print(f"[{stamp}] {m}", flush=True))
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
        if args.once:
            print()
            summarise(ledger, pool)
            return 0
        time.sleep(args.interval)


def main():
    p = argparse.ArgumentParser(prog="tdvms run", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ledger", default="tdvms_ledger.jsonl")
    add_args(p)
    try:
        return run(p.parse_args())
    except KeyboardInterrupt:
        print("\nstopped. Nothing is lost -- the ledger is the state, and every "
              "in-flight chunk is reclaimed by the next `run`.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
