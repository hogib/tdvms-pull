"""Enumerate a station's windows into the ledger. Idempotent, always.

    tdvms plan --station ELBA --start 2024-05-01 --end 2026-08-10 --chunk-days 21

Re-running never duplicates a chunk and never re-requests one. That matters more
than it sounds: extending a campaign's end date is the normal way to add work,
and a `plan` that re-added the windows already fetched would hand the queue
hundreds of requests for data sitting on disk.
"""
import argparse
import sys
from datetime import datetime, timedelta

from tdvms.ledger import Ledger, chunk_id

NAME = "plan"
HELP = "enumerate a station's chunks into the ledger (idempotent)"


def add_args(p):
    p.add_argument("--station", required=True, help="bare code, e.g. ELBA")
    p.add_argument("--start", default="2024-05-01")
    p.add_argument("--end", default="2026-08-10")
    p.add_argument("--chunk-days", type=int, default=21,
                   help="21 is what this campaign has run on throughout; longer "
                        "windows are not obviously worse but are unmeasured here")
    return p


def enumerate_chunks(ledger, station, start, end, chunk_days):
    """Appends the missing windows. Caller holds no lock; this takes it."""
    with ledger.locked():
        rows = ledger.rows()
        have = {chunk_id(r["station"], r["start"]) for r in rows}
        t, added = datetime.fromisoformat(start), 0
        stop = datetime.fromisoformat(end)
        while t < stop:
            nxt = min(t + timedelta(days=chunk_days), stop)
            if chunk_id(station, t.isoformat()) not in have:
                rows.append(ledger.add(station, t.isoformat(), nxt.isoformat()))
                added += 1
            t = nxt
        ledger._write(rows)
    return added


def run(args):
    ledger = Ledger(args.ledger)
    added = enumerate_chunks(ledger, args.station, args.start, args.end,
                             args.chunk_days)
    rows = ledger.rows()
    pending = sum(1 for r in rows if r["state"] == "pending")
    print(f"planned {added} new chunk(s) for {args.station} at {args.chunk_days} d")
    print(f"ledger holds {len(rows)} chunk(s), {pending} pending")
    return 0


def main():
    p = argparse.ArgumentParser(prog="tdvms plan", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ledger", default="tdvms_ledger.jsonl")
    add_args(p)
    return run(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())
