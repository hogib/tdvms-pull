"""Whole days of one station from an FDSN dataselect service, beside a campaign.

    tdvms fdsn --ledger sivrice_ledger.jsonl --station SVRC --start 2015-07-01 --end 2016-07-01
    tdvms fdsn --ledger sivrice_ledger.jsonl --station SVRC --days-file days.txt

TDVMS is not the only archive. KOERI (Kandilli) serves its own KO network over
FDSN web services, and holds days the portal answers "no data" for: SVRC has
July 2015 to June 2016 there and almost nothing at TDVMS. Unlike the portal it
answers at once -- no queue, no mail, no slots -- so this is a plain loop over
days, one GET each.

**State is a second ledger beside the campaign's**, `<name>_fdsn.jsonl` (or
`campaigns/<name>/fdsn.jsonl`): one row per station and UTC day, in the
ledger's own states (pending, claimed, fetched, nodata, failed), written under
its lock. The TUI's FDSN tab reads it. Running this again only adds the days
the ledger does not have, then works through whatever is still pending, so a
stopped pull carries on where it left off.

Each day is filed as `<out>/<STATION>/<NET>_<STA>_<DDMMYYYY>_000000_<DDMMYYYY>_000000_<band>.mseed`,
the name TDVMS gives its own archive members, so whatever reads TDVMS downloads
(`sfc split-days`) reads these too. The bytes are the service's own miniSEED,
untrimmed: records may run a few seconds past midnight.
"""
import argparse
import io
import json
import os
import pathlib
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

from tdvms import campaign, compat, coverage
from tdvms.ledger import TERMINAL, Ledger, chunk_id, now

NAME = "fdsn"
HELP = "fetch whole days of a station from an FDSN service (KOERI by default)"

KOERI = "http://eida.koeri.boun.edu.tr"
# Answered for a day the service holds nothing for. Asked for explicitly: the
# FDSN default is 204, which some proxies turn into an empty 200.
NODATA = (204, 404)
# The note on a day counted from a file already on disk rather than downloaded.
ADOPTED = "on disk before this pull"


def state_path(c):
    """The FDSN ledger of a campaign: beside its own ledger, in either layout."""
    p = c.path
    if p.name == "ledger.jsonl":
        return p.parent / "fdsn.jsonl"
    return p.parent / f"{c.name}_fdsn.jsonl"


def default_out(c):
    p = c.path
    if p.name == "ledger.jsonl":
        return p.parent / "fdsn_raw"
    return p.parent / f"{c.name}_fdsn_raw"


def run_lock(state):
    """Held by a running `tdvms fdsn` for as long as it works on `state`."""
    return state.with_name(state.name + ".run")


def holder(state):
    """What the live pull on this ledger says about itself, or None.

    Probes the lock rather than trusting the file: the text outlives its
    writer, the lock does not.
    """
    path = run_lock(state)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        if compat.held(fh):
            try:
                return json.loads(fh.read())
            except ValueError:
                return {}
    return None


def file_name(network, station, day, channels):
    nxt = day + timedelta(days=1)
    return (f"{network}_{station}_{day:%d%m%Y}_000000_{nxt:%d%m%Y}_000000_"
            f"{channels[:2]}.mseed")


def day_hours(data, day):
    """`{channel: hours}` of data inside the UTC day, from miniSEED bytes."""
    lo = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    out = {}
    for cid, (_, spans) in coverage.scan(io.BytesIO(data)).items():
        hours = coverage.length(coverage.clip(spans, lo, lo + 86400)) / 3600
        out[cid.rsplit(".", 1)[-1]] = round(hours, 2)
    return out


def plan(ledger, station, days, network, channels, service):
    """Adds a pending row for each day the ledger does not hold yet."""
    with ledger.locked():
        rows = ledger.rows()
        have = {chunk_id(r["station"], r["start"]) for r in rows}
        added = 0
        for d in sorted(set(days)):
            start = datetime(d.year, d.month, d.day).isoformat()
            if chunk_id(station, start) in have:
                continue
            row = ledger.add(station, start, (datetime.fromisoformat(start)
                                              + timedelta(days=1)).isoformat())
            row.update(network=network, channels=channels, service=service,
                       hours=None, path=None)
            rows.append(row)
            added += 1
        ledger._write(rows)
    return added


def adopt(ledger, out_dir):
    """Marks fetched every pending day whose file is already on disk.

    The way an earlier pull's files -- this one's, or a script's that named
    them the same way -- are counted instead of downloaded again.
    """
    adopted = 0
    for r in ledger.in_state("pending"):
        day = date.fromisoformat(r["start"][:10])
        path = pathlib.Path(out_dir) / r["station"] / file_name(
            r["network"], r["station"], day, r["channels"])
        if not path.exists():
            continue
        data = path.read_bytes()
        hours = day_hours(data, day)
        ledger.update(chunk_id(r["station"], r["start"]), state="fetched", path=str(path),
                      bytes=len(data), hours=hours, fetched_at=now(),
                      note=ADOPTED)
        adopted += 1
    return adopted


def fetch_day(session, row, timeout):
    """The day's miniSEED bytes, or None when the service holds none.

    Raises:
        requests.RequestException: The service did not answer, or answered
            with an error. The caller retries.
    """
    start = datetime.fromisoformat(row["start"])
    resp = session.get(f"{row['service'].rstrip('/')}/fdsnws/dataselect/1/query",
                       params={"network": row["network"], "station": row["station"],
                               "location": "*", "channel": row["channels"],
                               "starttime": start.isoformat(),
                               "endtime": (start + timedelta(days=1)).isoformat(),
                               "nodata": 404},
                       timeout=timeout)
    if resp.status_code in NODATA:
        return None
    resp.raise_for_status()
    return resp.content or None


def work(ledger, out_dir, retries=5, timeout=300, pause=30, session=None, log=print):
    """Works through every pending day, oldest first. Returns the outcome counts."""
    session = session or requests.Session()
    counts = {}
    while True:
        todo = sorted(ledger.in_state("pending"), key=lambda r: (r["start"], r["station"]))
        if not todo:
            return counts
        r = todo[0]
        cid = chunk_id(r["station"], r["start"])
        day = date.fromisoformat(r["start"][:10])
        ledger.update(cid, state="claimed", claimed_at=now(),
                      attempts=(r.get("attempts") or 0) + 1)
        data, error = None, None
        for attempt in range(1, retries + 1):
            try:
                data, error = fetch_day(session, r, timeout), None
                break
            except requests.RequestException as e:
                # KOERI resets connections now and then, mid-transfer too.
                error = f"{type(e).__name__}: {e}"[:200]
                log(f"  {cid}  {type(e).__name__}, try {attempt}/{retries}")
                if attempt < retries:
                    time.sleep(pause * attempt)
        if error:
            ledger.update(cid, state="failed", fetched_at=now(), note=error)
            log(f"[FAILED] {cid}  {error}")
        elif data is None:
            ledger.update(cid, state="nodata", fetched_at=now(), note="no data at source")
            log(f"[NODATA] {cid}")
        else:
            path = pathlib.Path(out_dir) / r["station"] / file_name(
                r["network"], r["station"], day, r["channels"])
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            compat.replace(tmp, path)
            hours = day_hours(data, day)
            ledger.update(cid, state="fetched", fetched_at=now(), path=str(path),
                          bytes=len(data), hours=hours, note=None)
            log(f"[OK] {cid}  " + "  ".join(f"{c} {h:.1f} h" for c, h in sorted(hours.items())))
        state = ledger.by_id(cid)["state"]
        counts[state] = counts.get(state, 0) + 1


def reclaim(ledger):
    """Puts back to pending what a pull that died mid-day left claimed."""
    n = 0
    for r in ledger.in_state("claimed"):
        ledger.update(chunk_id(r["station"], r["start"]), state="pending", claimed_at=None)
        n += 1
    return n


def days_from(args):
    days = [date.fromisoformat(d) for d in args.days or []]
    if args.days_file:
        text = pathlib.Path(args.days_file).read_text(encoding="utf-8")
        days += [date.fromisoformat(d) for d in text.split()]
    if args.start or args.end:
        if not (args.start and args.end):
            sys.exit("[ERROR] --start and --end go together")
        d, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
        while d < end:
            days.append(d)
            d += timedelta(days=1)
    return days


def add_args(p):
    p.add_argument("--station", required=True, help="bare code, e.g. SVRC")
    p.add_argument("--network", default="KO", help="network code (default KO)")
    p.add_argument("--channels", default="BH?", help="channel pattern (default BH?)")
    p.add_argument("--service", default=KOERI,
                   help=f"the FDSN service's base URL (default {KOERI})")
    p.add_argument("--start", help="first day, YYYY-MM-DD")
    p.add_argument("--end", help="the day after the last, YYYY-MM-DD")
    p.add_argument("--day", action="append", dest="days", help="one day; repeatable")
    p.add_argument("--days-file", help="a file of YYYY-MM-DD days, separated by whitespace")
    p.add_argument("--out-dir", help="where the days are filed (default: <name>_fdsn_raw "
                                     "beside the ledger, or the campaign's fdsn_raw)")
    p.add_argument("--retries", type=int, default=5,
                   help="tries per day before it is recorded failed (default 5)")
    p.add_argument("--timeout", type=float, default=300,
                   help="seconds to wait for one day's answer (default 300)")
    p.add_argument("--retry-failed", action="store_true",
                   help="put this station's failed days back to pending first")
    p.add_argument("--plan-only", action="store_true",
                   help="add the days to the FDSN ledger and stop")
    return p


def run(args):
    c = campaign.for_ledger(args.ledger)
    state = state_path(c)
    out_dir = pathlib.Path(args.out_dir) if args.out_dir else default_out(c)
    station = args.station.rsplit(".", 1)[-1].strip().upper()
    ledger = Ledger(state)

    lock = run_lock(state)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock, "a+", encoding="utf-8")
    try:
        compat.lock(fh, block=False)
    except BlockingIOError:
        fh.seek(0)
        sys.exit(f"[ERROR] another `tdvms fdsn` is working on {state} "
                 f"({fh.read().strip() or 'unknown process'}). Stop it first.")
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps({"pid": os.getpid(), "station": station, "service": args.service,
                         "out_dir": str(out_dir), "started": now()}) + "\n")
    fh.flush()

    added = plan(ledger, station, days_from(args), args.network, args.channels, args.service)
    back = reclaim(ledger)
    if args.retry_failed:
        for r in ledger.in_state("failed"):
            if r["station"] == station:
                ledger.update(chunk_id(r["station"], r["start"]), state="pending", note=None)
                back += 1
    adopted = adopt(ledger, out_dir)
    rows = ledger.rows()
    print(f"{state}: {added} day(s) added, {adopted} already on disk, "
          f"{back} back to pending; {sum(r['state'] == 'pending' for r in rows)} to fetch "
          f"from {args.service} into {out_dir}", flush=True)
    if args.plan_only:
        return 0

    def log(msg):
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

    counts = work(ledger, out_dir, args.retries, args.timeout, log=log)
    rows = ledger.rows()
    left = sum(r["state"] not in TERMINAL for r in rows)
    print("done: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
          + (f"; {left} not finished" if left else ""), flush=True)
    return 0


def main():
    compat.utf8_output()
    p = argparse.ArgumentParser(prog="tdvms fdsn", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ledger", default="tdvms_ledger.jsonl",
                   help="the campaign this pull belongs to; its FDSN ledger sits beside it")
    add_args(p)
    try:
        return run(p.parse_args())
    except KeyboardInterrupt:
        print("stopped. The FDSN ledger is the state; the next `tdvms fdsn` carries on.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
