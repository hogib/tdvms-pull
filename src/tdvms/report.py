"""Where the campaign stands, including the parts that are stuck.

    tdvms status
    tdvms status --slots 8 --address you@gmail.com    # the slot table too

A status line that only counts states hides the failures this tool exists to
catch: a slot held against a request nobody made counts as one `claimed` row,
which looks like progress. So anything stalled is named, with its age.
"""
import argparse
import sys

from tdvms.ledger import IN_FLIGHT, Ledger, age_seconds, chunk_id

NAME = "status"
HELP = "what the campaign has done, and what is stuck"

ORDER = ("pending", "claimed", "submitted", "fetched", "nodata", "failed", "retired")


def turnaround(rows):
    """Median and range of submit-to-fetch, in minutes, over rows that have both.

    Rows whose stamps invert are skipped rather than reported. The old tool
    subtracted a `fetched_at` left behind by a reset from a `submitted_at`
    belonging to a later attempt and printed `range -3064-76 min` -- not a fast
    request, arithmetic across two unrelated events.
    """
    from datetime import datetime
    laps = []
    for r in rows:
        sub, fet = r.get("submitted_at"), r.get("fetched_at")
        if not (sub and fet):
            continue
        try:
            delta = (datetime.fromisoformat(fet) - datetime.fromisoformat(sub))
        except ValueError:
            continue
        minutes = delta.total_seconds() / 60
        if minutes < 0:
            continue
        laps.append(minutes)
    if not laps:
        return None
    laps.sort()
    return laps[len(laps) // 2], laps[0], laps[-1], len(laps)


def summarise(ledger, pool=None, out=print):
    rows = ledger.rows()
    if not rows:
        out(f"{ledger.path} is empty — run `tdvms plan` first")
        return 0
    by = {}
    for r in rows:
        by.setdefault(r["state"], []).append(r)

    out(f"{len(rows)} chunk(s) in {ledger.path}")
    for state in ORDER:
        got = by.get(state)
        if not got:
            continue
        gb = sum(r.get("bytes") or 0 for r in got) / 1e9
        out(f"  {state:10s} {len(got):4d}" + (f"   {gb:.1f} GB" if gb else ""))

    for r in by.get("submitted", []):
        age = age_seconds(r.get("submitted_at"))
        when = f"{age/3600:.1f} h" if age is not None else "unstamped"
        out(f"  awaiting link: {chunk_id(r['station'], r['start'])} "
            f"({r.get('email')}) — {when}")
    for r in by.get("claimed", []):
        age = age_seconds(r.get("claimed_at"))
        when = f"{age/60:.0f} min" if age is not None else "unstamped"
        out(f"  STUCK in claimed: {chunk_id(r['station'], r['start'])} "
            f"({r.get('email')}) — {when}; `run` reclaims this automatically")
    for r in by.get("failed", []):
        out(f"  FAILED: {chunk_id(r['station'], r['start'])} — {r.get('note')}")

    # Per station, because "46 nodata" across the whole ledger hides that all
    # 26 of one station's belong to a station that has never returned anything.
    out("\n  per station:")
    for station in ledger.stations():
        got = [r for r in rows if r["station"] == station]
        counts = {}
        for r in got:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        gb = sum(r.get("bytes") or 0 for r in got if r["state"] == "fetched") / 1e9
        detail = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        flag = ""
        if not counts.get("fetched") and counts.get("nodata", 0) >= 8:
            flag = "   <- never returned data"
        out(f"    {station:6s} {len(got):3d}  {gb:5.1f} GB  {detail}{flag}")

    lap = turnaround(rows)
    if lap:
        mid, lo, hi, n = lap
        out(f"\n  turnaround  n={n}  median {mid:.0f} min  range {lo:.0f}-{hi:.0f} min")

    if pool is not None:
        pool.refresh(ledger)
        out(f"\n  slots ({len(pool)} address(es)):")
        for slot in pool.slots.values():
            if slot.holding:
                r = slot.holding
                stamp = r.get("submitted_at") or r.get("claimed_at")
                age = age_seconds(stamp)
                when = f"{age/3600:.1f} h" if age is not None else "unstamped"
                where = f"{r['state']} {chunk_id(r['station'], r['start'])}  {when}"
            elif slot.remote_busy:
                where = "cooling down after a portal BUSY"
            else:
                where = "free"
            out(f"    {slot.email:38s} {where}")
        free = len(pool.free())
        out(f"    {free} of {len(pool)} free")
    return 0


def add_args(p):
    p.add_argument("--address", default=None,
                   help="base address; with --slots, prints the slot table too")
    p.add_argument("--slots", type=int, default=None)
    return p


def run(args):
    pool = None
    if args.address and args.slots:
        from tdvms.slots import Pool
        pool = Pool(args.address, args.slots)
    return summarise(Ledger(args.ledger), pool)


def main():
    p = argparse.ArgumentParser(prog="tdvms status", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ledger", default="tdvms_ledger.jsonl")
    add_args(p)
    return run(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())
