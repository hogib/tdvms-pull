"""Enumerate a station's windows into the ledger. Idempotent, always.

    tdvms plan --station ELBA --start 2024-05-01 --end 2026-08-10 --chunk-days 21
    tdvms plan --station ELBA --start 2024-05-01 --end 2026-08-10 --every-days 7

The second samples rather than covers: one 1-day window every week.

Re-running never duplicates a chunk and never re-requests one. That matters more
than it sounds: extending a campaign's end date is the normal way to add work,
and a `plan` that re-added the windows already fetched would hand the queue
hundreds of requests for data sitting on disk.
"""
import argparse
import sys
from datetime import datetime, timedelta

from tdvms import compat, history
from tdvms.ledger import Ledger, chunk_id

NAME = "plan"
HELP = "enumerate a station's chunks into the ledger (idempotent)"

CHUNK_DAYS = 21           # window length when covering the whole period
SAMPLE_DAYS = 1           # window length when sampling with --every-days


def normalise(code):
    """`"TU.KAND"` -> `"KAND"`. The portal lists BARE codes; none has a dot.

    The network is already in the submission payload (`"networks": ["TU"]`), so
    a network-qualified code reaches the portal as a station name that cannot
    exist and every window fails permanently. A ledger planned as `TU.KAND` had
    all 40 of its chunks rejected that way -- and each rejection looked like a
    station problem rather than a typo, because the portal's answer is the same
    either way.
    """
    return code.rsplit(".", 1)[-1].strip().upper()


def verify(station, client=None):
    """Refuses a station the portal does not list, and says what it does list.

    Checked at PLAN time, where it costs one round trip and the fix is to retype
    a word. Left to submission time it costs one queue slot per window, and the
    error arrives once per cycle for as long as the campaign runs.

    Raises:
        SystemExit: If the code is not listed. Near-matches are printed, which
            is what turns "not in the station list" into an actionable message.
    """
    from tdvms.client import STRONG_MOTION, Client
    client = client or Client()
    try:
        codes = client.station_codes()
    except Exception as e:
        # The portal being unreachable is not a reason to refuse to plan --
        # planning is local bookkeeping and the campaign may be offline.
        print(f"  [!] could not reach the station list ({type(e).__name__}); "
              f"planning {station} unverified")
        return
    if station in codes:
        try:
            net = client.station_network(station)
            dev = client.device_code(station)
            if net != "TU" or dev != "H":
                kind = (" -- strong motion, an accelerometer rather than a "
                        "broadband" if net in STRONG_MOTION else "")
                print(f"  [i] {station} is network {net}, instrument {dev}{kind}")
        except Exception:
            pass
        return
    near = [c for c in codes if c.startswith(station[:2])][:8]
    sys.exit(f"[ERROR] the portal does not list {station!r}.\n"
             f"        It lists {len(codes)} stations across "
             f"{', '.join(client.netcodes)}, bare codes with no network "
             f"prefix.\n"
             f"        Closest by prefix: {', '.join(near) or '(none)'}\n"
             f"        Pass --no-verify to plan it anyway.")


def add_args(p):
    p.add_argument("--station", required=True, help="bare code, e.g. ELBA")
    p.add_argument("--no-verify", dest="verify", action="store_false",
                   help="skip the check that the portal lists this station. The "
                        "check costs one round trip here and saves one queue "
                        "slot per window later.")
    p.add_argument("--start", default="2024-05-01")
    p.add_argument("--end", default="2026-08-10")
    p.add_argument("--chunk-days", type=int, default=None,
                   help=f"window length; {CHUNK_DAYS} by default, {SAMPLE_DAYS} "
                        f"with --every-days. {CHUNK_DAYS} is what this campaign has "
                        f"run on throughout; longer windows are not obviously worse "
                        f"but are unmeasured here")
    p.add_argument("--every-days", type=int, default=None,
                   help="sample instead of covering: start a window every N days. "
                        "--every-days 7 pulls one day a week")
    return p


def windows(start, end, chunk_days, every_days=None):
    """The `(start, end)` pairs to plan, as ISO strings.

    Back to back by default. With `every_days`, a window opens every that many
    days and the time between windows is never requested -- a sample of the
    period rather than all of it. The last window stops at `end` either way.

    Raises:
        ValueError: On a window under a day, or one longer than its stride,
            which would request the overlap twice.
    """
    if every_days is None:
        every_days = chunk_days
    if chunk_days < 1:
        raise ValueError("a window is at least one day")
    if every_days < chunk_days:
        raise ValueError(f"{chunk_days}-day windows every {every_days} days "
                         f"would overlap")
    t, stop, out = datetime.fromisoformat(start), datetime.fromisoformat(end), []
    while t < stop:
        out.append((t.isoformat(), min(t + timedelta(days=chunk_days), stop).isoformat()))
        t += timedelta(days=every_days)
    return out


def enumerate_chunks(ledger, station, start, end, chunk_days, every_days=None):
    """Appends the missing windows. Caller holds no lock; this takes it."""
    spans = windows(start, end, chunk_days, every_days)
    with ledger.locked():
        rows = ledger.rows()
        have = {chunk_id(r["station"], r["start"]) for r in rows}
        added = 0
        for s, e in spans:
            if chunk_id(station, s) not in have:
                rows.append(ledger.add(station, s, e))
                added += 1
        ledger._write(rows)
    return added


def run(args):
    station = normalise(args.station)
    if station != args.station:
        print(f"  station {args.station!r} -> {station!r} "
              f"(the portal lists bare codes; the network is in the payload)")
    if args.verify:
        verify(station)
    warning = history.gather(history.around(args.ledger)).get(station)
    if warning and warning.warning():
        print(f"  [!] {warning.warning()}\n"
              f"      planned anyway -- no-data is about a period, not forever")
    chunk_days = args.chunk_days
    if chunk_days is None:
        chunk_days = SAMPLE_DAYS if args.every_days else CHUNK_DAYS
    ledger = Ledger(args.ledger)
    try:
        added = enumerate_chunks(ledger, station, args.start, args.end,
                                 chunk_days, args.every_days)
    except ValueError as e:
        sys.exit(f"[ERROR] {e}")
    rows = ledger.rows()
    pending = sum(1 for r in rows if r["state"] == "pending")
    every = f", one every {args.every_days} d" if args.every_days else ""
    print(f"planned {added} new chunk(s) for {station} at {chunk_days} d{every}")
    print(f"ledger holds {len(rows)} chunk(s), {pending} pending")
    return 0


def main():
    compat.utf8_output()
    p = argparse.ArgumentParser(prog="tdvms plan", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ledger", default="tdvms_ledger.jsonl")
    add_args(p)
    return run(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())
