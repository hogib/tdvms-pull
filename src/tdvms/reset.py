"""Put one chunk back to pending, by identity.

    tdvms reset --chunk ELBA:2025-09-09
    tdvms reset --station ELBA --start 2025-09-09

**A chunk is `station:start`, and this is why.** The tool this replaces matched
`reset --start <date>` against every row in the file whose start began with that
date, across all stations. One command meant for a single ELBA window hit three
rows in two stations, two of which had already been answered `nodata`, and the
slot spent re-requesting them was gone. There is no way to express that here:
without `--station`, an ambiguous date is refused and the candidates are listed.
"""
import argparse
import sys

from tdvms.ledger import Ledger, chunk_id

NAME = "reset"
HELP = "requeue one chunk, identified by station and start"


def add_args(p):
    p.add_argument("--chunk", default=None, help="STATION:START, e.g. ELBA:2025-09-09")
    p.add_argument("--station", default=None)
    p.add_argument("--start", default=None, help="ISO date or full timestamp")
    p.add_argument("--note", default=None)
    return p


def resolve(ledger, station, start):
    """The single row this names, or a SystemExit listing the ambiguity."""
    rows = [r for r in ledger.rows()
            if (station is None or r["station"] == station)
            and r["start"].startswith(start)]
    if not rows:
        sys.exit(f"[ERROR] no chunk matches "
                 f"{station or '<any station>'} starting {start}")
    if len(rows) > 1:
        listing = "\n".join(f"    {chunk_id(r['station'], r['start'])}  {r['state']}"
                            for r in rows)
        sys.exit(f"[ERROR] {start} matches {len(rows)} chunks across "
                 f"{len({r['station'] for r in rows})} station(s):\n{listing}\n"
                 f"  Name one with --chunk STATION:START. Resetting all of them "
                 f"is how a date-keyed reset burned a queue slot on windows that "
                 f"were already answered.")
    return rows[0]


def run(args):
    ledger = Ledger(args.ledger)
    if args.chunk:
        station, _, start = args.chunk.partition(":")
        if not start:
            sys.exit(f"[ERROR] --chunk wants STATION:START, got {args.chunk!r}")
    elif args.start:
        station, start = args.station, args.start
    else:
        sys.exit("[ERROR] give --chunk STATION:START, or --station and --start")
    row = resolve(ledger, station, start)
    cid = chunk_id(row["station"], row["start"])
    print(f"  {cid}: {row['state']} -> pending")
    ledger.release(cid, note=args.note)
    return 0


def main():
    p = argparse.ArgumentParser(prog="tdvms reset", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ledger", default="tdvms_ledger.jsonl")
    add_args(p)
    return run(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())
