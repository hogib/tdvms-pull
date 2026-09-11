"""Import an existing `afad_campaign` ledger without touching it.

    tdvms adopt --from ../cnn_earthquake/afad_campaign_ledger.jsonl

The source file is opened read-only and never written. A live campaign can keep
running against it while the two ledgers are compared side by side, which is the
only safe way to swap the tool driving a queue that takes days to drain.

Three things are repaired on the way in:

* **`attempts`** defaults to 0. The old schema had no retry counter, so every
  adopted row starts with a clean budget.
* **Inverted stamps are dropped.** Rows reset by the old tool kept their
  `fetched_at` while losing their `submitted_at`, and the status line then
  subtracted two unrelated events and printed `range -3064-76 min`.
* **`claimed_at` is stamped now, not invented.** A row adopted as `claimed` has
  no record of when it was claimed; dating it to the import is honest and lets
  the reaper time it out one interval later instead of immediately.
"""
import argparse
import json
import pathlib
import sys

from tdvms.ledger import Ledger, age_seconds, now

NAME = "adopt"
HELP = "import an afad_campaign ledger (read-only on the source)"

FIELDS = ("station", "start", "end", "state", "email", "url", "bytes", "note")


def convert(rows, stamp=None):
    """Old rows -> new rows. Pure, so the repairs are testable."""
    stamp = stamp or now()
    out, repairs = [], []
    for r in rows:
        row = {k: r.get(k) for k in FIELDS}
        row["attempts"] = r.get("attempts", 0) or 0
        sub, fet = r.get("submitted_at"), r.get("fetched_at")
        if sub and fet and fet < sub:
            repairs.append(f"{r['station']}:{r['start'][:10]} fetched before submitted")
            sub = fet = None
        if fet and not sub:
            # A fetch with no submission is the same inversion seen from the
            # other side: the reset cleared one stamp and left the other.
            repairs.append(f"{r['station']}:{r['start'][:10]} fetched with no submission")
            fet = None
        row["submitted_at"], row["fetched_at"] = sub, fet
        row["claimed_at"] = r.get("claimed_at")
        if row["state"] == "claimed" and not row["claimed_at"]:
            row["claimed_at"] = stamp
            repairs.append(f"{r['station']}:{r['start'][:10]} claimed with no stamp")
        out.append(row)
    return out, repairs


def add_args(p):
    p.add_argument("--from", dest="source", required=True,
                   help="the afad_campaign_ledger.jsonl to import (never written)")
    p.add_argument("--force", action="store_true",
                   help="overwrite a ledger that already has chunks in it")
    return p


def run(args):
    src = pathlib.Path(args.source)
    if not src.exists():
        sys.exit(f"[ERROR] {src} does not exist")
    ledger = Ledger(args.ledger)
    if ledger.rows() and not args.force:
        sys.exit(f"[ERROR] {ledger.path} already holds {len(ledger.rows())} chunk(s). "
                 f"Adopting would replace them; pass --force if that is intended.")
    old = [json.loads(l) for l in src.read_text().splitlines() if l.strip()]
    rows, repairs = convert(old)
    with ledger.locked():
        ledger._write(rows)
    by = {}
    for r in rows:
        by[r["state"]] = by.get(r["state"], 0) + 1
    print(f"adopted {len(rows)} chunk(s) from {src}  (source untouched)")
    for state in sorted(by):
        print(f"  {state:10s} {by[state]:4d}")
    if repairs:
        print(f"\n  {len(repairs)} row(s) repaired on import:")
        for r in repairs[:10]:
            print(f"    {r}")
        if len(repairs) > 10:
            print(f"    ... and {len(repairs) - 10} more")
    print(f"\nwrote {ledger.path}")
    return 0


def main():
    p = argparse.ArgumentParser(prog="tdvms adopt", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ledger", default="tdvms_ledger.jsonl")
    add_args(p)
    return run(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())
