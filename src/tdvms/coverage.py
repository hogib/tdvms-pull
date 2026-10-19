"""What the downloaded archives actually cover, and where the holes are.

    tdvms coverage --ledger silivri_ledger.jsonl
    tdvms coverage --ledger campaigns/van-2011/ledger.jsonl --gaps --min-gap 60

A `fetched` window says an archive arrived, not that it is whole. Stations drop
out for minutes or days inside a window, and whether a pair is usable depends on
exactly those holes. So this reads every archive's miniSEED records and reports,
per station and channel, how much of the requested period is really there --
and for each gap, why: a hole inside an archive, a window the portal had no data
for, or one not downloaded yet.

It reads only record *headers* -- start time, sample count, sample rate -- with
the standard library, never the samples. TDVMS serves miniSEED 2 (big-endian,
blockette 1000, 512-byte records), so the timing of every record is in a fixed
48-byte header and no decoder is needed; ObsPy would be a 100 MB dependency to
read twelve fields. Results are cached per archive in `<raw>/.coverage.json`,
keyed by size and modification time, so only new archives are read again.
"""
import argparse
import calendar
import json
import pathlib
import struct
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

from tdvms import campaign
from tdvms.ledger import IN_FLIGHT, Ledger

NAME = "coverage"
HELP = "what the downloaded archives actually cover, and the gaps"

CACHE = ".coverage.json"
HEADER = struct.Struct(">6sc1x5s2s3s2sHHBBB1xHHhhBBBBiHH")
HEADER_LE = struct.Struct("<6sc1x5s2s3s2sHHBBB1xHHhhBBBBiHH")


# --- reading records ------------------------------------------------------------

def _rate(factor, mult):
    """Samples per second from the header's factor and multiplier (SEED 2.4)."""
    if factor > 0:
        return factor * mult if mult > 0 else factor / -mult if mult < 0 else factor
    if factor < 0:
        return -mult / factor if mult > 0 else 1 / (factor * mult) if mult < 0 else -1 / factor
    return 0.0


_YEARS = {}


def _epoch(year, doy, hour, minute, sec, frac):
    base = _YEARS.get(year)
    if base is None:
        base = _YEARS[year] = calendar.timegm((year, 1, 1, 0, 0, 0))
    return base + (doy - 1) * 86400 + hour * 3600 + minute * 60 + sec + frac * 1e-4


def _record_length(data, off, first_blockette, count, order):
    """From blockette 1000, which every TDVMS record carries."""
    b = first_blockette
    for _ in range(count):
        if not b or off + b + 7 > len(data):
            break
        kind, nxt = struct.unpack_from(order + "HH", data, off + b)
        if kind == 1000:
            return 1 << data[off + b + 6]
        b = nxt
    return None


def scan(stream, block=1 << 22):
    """`{channel id: (rate, [[start, end], ...])}` from a miniSEED 2 byte stream.

    Spans are merged as they are read: a record that starts within 1.5 sample
    periods of where the last one ended continues it.
    """
    spans = {}
    pending = b""
    while True:
        chunk = stream.read(block)
        data = pending + chunk if pending else chunk
        off, n = 0, len(data)
        while n - off >= 48:
            hdr = HEADER.unpack_from(data, off)
            order = ">"
            if not 1900 <= hdr[6] <= 2100:
                hdr = HEADER_LE.unpack_from(data, off)
                order = "<"
            (_, quality, sta, loc, cha, net, year, doy, hour, minute, sec, frac,
             nsamp, factor, mult, activity, _, _, nblk, corr, _, blk) = hdr
            if quality not in b"DRQM" or not 1900 <= year <= 2100:
                raise ValueError(f"not a miniSEED 2 record at byte {off}")
            reclen = _record_length(data, off, blk, nblk, order)
            if reclen is None:
                if n - off < 64:
                    break         # the blockette may be in the next read
                raise ValueError("a record without blockette 1000; length unknown")
            if n - off < reclen:
                break
            rate = _rate(factor, mult)
            if nsamp and rate:
                start = _epoch(year, doy, hour, minute, sec, frac)
                if not activity & 0x02:          # correction not yet applied
                    start += corr * 1e-4
                end = start + nsamp / rate
                cid = ".".join(x.decode("ascii", "replace").strip()
                               for x in (net, sta, loc, cha))
                entry = spans.get(cid)
                if entry is None:
                    spans[cid] = (rate, [[start, end]])
                else:
                    last = entry[1][-1]
                    if last[0] <= start <= last[1] + 1.5 / rate:
                        if end > last[1]:
                            last[1] = end
                    else:
                        entry[1].append([start, end])
            off += reclen
        pending = data[off:]
        if not chunk:
            break
    return {cid: (rate, merge(s, 1.5 / rate)) for cid, (rate, s) in spans.items()}


def scan_archive(path):
    """Every miniSEED member of one archive, merged per channel."""
    out = {}
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if not info.filename.lower().endswith(".mseed"):
                continue
            with zf.open(info) as fh:
                for cid, (rate, spans) in scan(fh).items():
                    prev = out.get(cid)
                    out[cid] = (rate, merge((prev[1] if prev else []) + spans, 1.5 / rate))
    return out


# --- interval arithmetic ----------------------------------------------------------

def merge(spans, tol=0.0):
    """Sorted, with overlapping or touching spans joined."""
    out = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1] + tol:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def clip(spans, lo, hi):
    return [[max(s, lo), min(e, hi)] for s, e in spans if e > lo and s < hi]


def subtract(wanted, have):
    """The parts of `wanted` not covered by `have`; both merged lists."""
    out, j = [], 0
    for s, e in wanted:
        cur = s
        while j < len(have) and have[j][1] <= cur:
            j += 1
        k = j
        while k < len(have) and have[k][0] < e:
            if have[k][0] > cur:
                out.append([cur, have[k][0]])
            cur = max(cur, have[k][1])
            k += 1
        if cur < e:
            out.append([cur, e])
    return out


def length(spans):
    return sum(e - s for s, e in spans)


# --- the report ---------------------------------------------------------------------

@dataclass
class Gap:
    start: float
    end: float
    why: str          # "data gap", "no data at source", "not downloaded yet", "unavailable"

    @property
    def seconds(self):
        return self.end - self.start


@dataclass
class Channel:
    cid: str
    rate: float
    covered: float                    # seconds of the requested period present
    requested: float
    spans: list
    gaps: list = field(default_factory=list)

    @property
    def fraction(self):
        return self.covered / self.requested if self.requested else 0.0

    @property
    def label(self):
        """`HHZ`, or `00.HHZ` where there is a location code."""
        _, _, loc, cha = self.cid.split(".")
        return f"{loc}.{cha}" if loc else cha


@dataclass
class Station:
    code: str
    windows: list                     # [(start, end, state)]
    channels: list = field(default_factory=list)

    @property
    def requested(self):
        return merge([[s, e] for s, e, _ in self.windows])

    @property
    def span(self):
        req = self.requested
        return (req[0][0], req[-1][1]) if req else (0, 0)

    def state_at(self, t):
        return next((st for s, e, st in self.windows if s <= t < e), None)


def _ts(iso):
    t = datetime.fromisoformat(iso)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.timestamp()


def _why(state):
    if state == "fetched":
        return "data gap"
    if state == "nodata":
        return "no data at source"
    if state == "pending" or state in IN_FLIGHT:
        return "not downloaded yet"
    return "unavailable"


def _gaps(windows, have):
    """What `have` misses of each window, each part named for its own window.

    Cut at window boundaries, because one hole can run from a fetched window
    into one the portal had no data for, and those are different answers.
    Neighbouring parts with the same reason are joined again.
    """
    out = []
    for s, e, state in windows:
        for gs, ge in subtract([[s, e]], clip(have, s, e)):
            why = _why(state)
            if out and out[-1].why == why and abs(out[-1].end - gs) < 1e-6:
                out[-1].end = ge
            else:
                out.append(Gap(gs, ge, why))
    return out


def archive_path(raw, row):
    return pathlib.Path(raw) / row["station"] / f"{row['station']}_{row['start'][:10]}.zip"


def report(ledger, raw, stations=None, refresh=False, progress=None):
    """`[Station]` for a ledger whose archives are filed under `raw`.

    Args:
        progress: called as `progress(done, total, name)` while archives are read.
    """
    raw = pathlib.Path(raw)
    rows = [r for r in ledger.rows() if not stations or r["station"] in stations]
    cache_path = raw / CACHE
    try:
        cache = {} if refresh else json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cache = {}
    fetched = [(r, archive_path(raw, r)) for r in rows if r["state"] == "fetched"]
    fetched = [(r, p) for r, p in fetched if p.exists()]
    scanned = {}
    for i, (r, p) in enumerate(fetched):
        st = p.stat()
        key = str(p.relative_to(raw))
        hit = cache.get(key)
        if hit and hit["size"] == st.st_size and hit["mtime"] == st.st_mtime:
            scanned[key] = {cid: (c["rate"], c["spans"]) for cid, c in hit["channels"].items()}
        else:
            if progress:
                progress(i, len(fetched), p.name)
            try:
                got = scan_archive(p)
            except (ValueError, zipfile.BadZipFile, OSError):
                got = {}
            scanned[key] = got
            cache[key] = {"size": st.st_size, "mtime": st.st_mtime,
                          "channels": {cid: {"rate": rate, "spans": spans}
                                       for cid, (rate, spans) in got.items()}}
    if progress:
        progress(len(fetched), len(fetched), "")
    if raw.is_dir():
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache))
        tmp.replace(cache_path)

    out = []
    for code in sorted({r["station"] for r in rows}):
        mine = [r for r in rows if r["station"] == code]
        st = Station(code, sorted((_ts(r["start"]), _ts(r["end"]), r["state"])
                                  for r in mine))
        requested = st.requested
        per_channel = {}
        for r in mine:
            if r["state"] != "fetched":
                continue
            for cid, (rate, spans) in scanned.get(str(archive_path(raw, r).relative_to(raw)),
                                                  {}).items():
                prev = per_channel.get(cid, (rate, []))
                per_channel[cid] = (rate, prev[1] + spans)
        for cid in sorted(per_channel):
            rate, spans = per_channel[cid]
            have = merge(clip(merge(spans, 1.5 / rate), *st.span), 1.5 / rate)
            gaps = _gaps(st.windows, have)
            ch = Channel(cid, rate, length(requested) - sum(g.seconds for g in gaps),
                         length(requested), have, gaps)
            st.channels.append(ch)
        out.append(st)
    return out


def timeline(station, channel, width):
    """`[(char, kind)]`, one per bucket across the station's requested time.

    The requested windows are laid end to end, so a campaign that asked for a
    few months in 2011 and a few in 2026 is drawn as those months, not as
    fifteen years of mostly nothing. kind is "full", "partial", "gap",
    "nodata", "pending", or "none".
    """
    wanted = station.requested
    total = length(wanted)
    if total <= 0 or width < 1:
        return []
    have = channel.spans if channel else []
    step = total / width
    cells, i, offset = [], 0, 0.0      # offset: requested time before wanted[i]
    for n in range(width):
        lo, hi = n * step, (n + 1) * step
        covered, why_len = 0.0, {}
        while i < len(wanted):
            s, e = wanted[i]
            seg_lo, seg_hi = max(lo, offset), min(hi, offset + (e - s))
            if seg_hi > seg_lo:
                a, b = s + (seg_lo - offset), s + (seg_hi - offset)
                covered += length(clip(have, a, b))
                why = _why(station.state_at((a + b) / 2))
                why_len[why] = why_len.get(why, 0) + (b - a)
            if offset + (e - s) <= hi:
                offset += e - s
                i += 1
            else:
                break
        f = covered / step
        if f >= 0.995:
            cells.append(("█", "full"))
        elif f > 0:
            cells.append(("▆" if f >= 0.75 else "▄" if f >= 0.5 else "▂", "partial"))
        else:
            why = max(why_len, key=why_len.get) if why_len else None
            cells.append({"data gap": ("▁", "gap"), "no data at source": ("×", "nodata"),
                          "not downloaded yet": ("·", "pending")}.get(why, (" ", "none")))
    return cells


def fmt_seconds(s):
    if s < 120:
        return f"{s:.0f} s"
    if s < 7200:
        return f"{s / 60:.0f} min"
    if s < 172800:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} d"


def fmt_time(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --- the command ---------------------------------------------------------------------

def add_args(p):
    p.add_argument("--out-dir", default=None,
                   help="where the archives are filed (default: the campaign's own folder)")
    p.add_argument("--station", action="append", default=None, dest="stations",
                   help="repeatable; only these stations")
    p.add_argument("--gaps", action="store_true", help="list every gap, not just the largest")
    p.add_argument("--min-gap", type=float, default=1.0,
                   help="ignore gaps shorter than this many seconds (default 1)")
    p.add_argument("--refresh", action="store_true",
                   help="read every archive again instead of using the cache")
    return p


def run(args):
    ledger = Ledger(args.ledger)
    if not ledger.rows():
        sys.exit(f"[ERROR] {ledger.path} is empty")
    raw = pathlib.Path(args.out_dir) if args.out_dir else campaign.for_ledger(ledger.path).raw

    def progress(done, total, name):
        if not sys.stderr.isatty():
            return
        if total and done < total:
            print(f"\r  reading {done + 1}/{total} {name[:40]:<40}", end="",
                  file=sys.stderr, flush=True)
        elif total:
            print("\r" + " " * 60 + "\r", end="", file=sys.stderr, flush=True)

    stations = report(ledger, raw, args.stations, args.refresh, progress)
    print(f"coverage under {raw}\n")
    print(f"  {'station':8} {'channel':14} {'covered':>8} {'of':>8}  {'gaps':>5}  largest gap")
    for st in stations:
        states = {}
        for _, _, s in st.windows:
            states[s] = states.get(s, 0) + 1
        if not st.channels:
            detail = ", ".join(f"{n} {s}" for s, n in sorted(states.items()))
            print(f"  {st.code:8} {'—':14} {'':>8} {'':>8}  {'':>5}  no archives ({detail})")
            continue
        for ch in st.channels:
            gaps = [g for g in ch.gaps if g.seconds >= args.min_gap]
            big = max(gaps, key=lambda g: g.seconds, default=None)
            largest = (f"{fmt_seconds(big.seconds)} at {fmt_time(big.start)[:16]} ({big.why})"
                       if big else "")
            print(f"  {st.code:8} {ch.label:14} {ch.fraction * 100:7.1f}% "
                  f"{fmt_seconds(ch.requested):>8}  {len(gaps):5}  {largest}")
            if args.gaps:
                for g in gaps:
                    print(f"      {fmt_time(g.start)} → {fmt_time(g.end)}  "
                          f"{fmt_seconds(g.seconds):>8}  {g.why}")
    return 0


def main():
    p = argparse.ArgumentParser(prog="tdvms coverage", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ledger", default="tdvms_ledger.jsonl")
    add_args(p)
    return run(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())
