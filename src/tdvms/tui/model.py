"""What the TUI shows and the repairs it makes. No Textual in here.

Everything a screen does that is not drawing lives here, so it can be tested
without a terminal: finding campaigns and the runs working them, planning, and
the two repairs the CLI already offers (`reset`, `ingest`) plus filing the
strays a shared inbox leaves behind.

**Monitor and repair only.** Nothing here starts or stops a run. A run is found
by reading `/proc`, and every write goes through `Ledger`'s own locked methods,
so the TUI is safe to leave open beside a live campaign.
"""
import os
import pathlib
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from tdvms import fetch as fetching
from tdvms.campaign import CAMPAIGNS, NAME_RE, Campaign, discover, new_campaign  # noqa: F401
from tdvms.campaign import for_ledger as campaign_for
from tdvms.ledger import IN_FLIGHT, TERMINAL, chunk_id, now
from tdvms.plan import enumerate_chunks
from tdvms.report import summarise
from tdvms.slots import Pool

SLOT_RE = re.compile(r"^(?P<local>[^+@]+)\+a(?P<n>\d+)@(?P<domain>.+)$", re.I)


# --- live runs ----------------------------------------------------------------

@dataclass
class LiveRun:
    pid: int
    ledger: pathlib.Path | None  # the campaign being worked; None once a queue is done
    address: str | None
    slots: int
    out_dir: str
    log: pathlib.Path | None     # wherever its stdout actually goes
    dry_run: bool
    command: str = "run"         # or "queue"
    queue: tuple = ()            # a queue's campaigns, in order, as ledger paths


# Options that take no value, so the word after one is not swallowed as it.
FLAGS = {"once", "dry-run", "claim-unknown", "no-retire", "exit-when-done"}


def command_args(argv):
    """`("run" | "queue", the arguments after it)` for a tdvms loop; else None.

    `uv run tdvms run ...` is a launcher whose child is the real loop, so the
    launcher itself is skipped -- otherwise every run would be counted twice.
    """
    if not argv or os.path.basename(argv[0]) in ("uv", "uvx"):
        return None
    for i, tok in enumerate(argv[:-1]):
        if os.path.basename(tok) == "tdvms" and argv[i + 1] in ("run", "queue"):
            return argv[i + 1], argv[i + 2:]
        if tok == "-m" and argv[i + 1] in ("tdvms.run", "tdvms.queue"):
            return argv[i + 1].split(".")[1], argv[i + 2:]
    return None


def run_args(argv):
    """The arguments after `run`, if this argv is a `tdvms run`; else None."""
    got = command_args(argv)
    return got[1] if got and got[0] == "run" else None


def parse(args):
    """`(options, positionals)`: `--key value` and `--key=value`, bare flags as ""."""
    out, rest, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            key, eq, val = a[2:].partition("=")
            if (not eq and key not in FLAGS and i + 1 < len(args)
                    and not args[i + 1].startswith("--")):
                val = args[i + 1]
                i += 1
            out[key] = val
        else:
            rest.append(a)
        i += 1
    return out, rest


def options(args):
    return parse(args)[0]


def _queued(specs, cwd, root):
    """A queue's campaigns, resolved as the queue itself resolved them."""
    out = []
    for spec in specs:
        p = cwd / spec
        if spec.endswith(".jsonl") or p.is_file():
            out.append(campaign_for(p))
            continue
        found = next((c for c in discover(root) if c.name == spec), None)
        if found:
            out.append(found)
    return out


def live_runs(proc="/proc"):
    """Every `tdvms run` and `tdvms queue` here. Empty with no `/proc` (macOS)."""
    runs = []
    if not pathlib.Path(proc).is_dir():
        return runs
    for d in pathlib.Path(proc).iterdir():
        if not d.name.isdigit():
            continue
        try:
            raw = (d / "cmdline").read_bytes()
            cwd = pathlib.Path(os.readlink(d / "cwd"))
        except OSError:
            continue
        got = command_args([a.decode(errors="replace") for a in raw.split(b"\0") if a])
        if got is None:
            continue
        command, args = got
        o, positional = parse(args)
        try:
            log = pathlib.Path(os.readlink(d / "fd" / "1"))
            log = log if log.is_absolute() and log.is_file() else None
        except OSError:
            log = None
        try:
            slots = int(o.get("slots") or 8)
        except ValueError:
            slots = 8
        if command == "queue":
            camps = _queued(positional, cwd, cwd / (o.get("root") or "."))
            # The queue works its campaigns in order and skips finished ones,
            # so the one it is on is the first that is not complete.
            now_on = next((c for c in camps if not c.ledger.complete()), None)
            runs.append(LiveRun(int(d.name), now_on.path.resolve() if now_on else None,
                                o.get("address") or None, slots,
                                str(now_on.raw) if now_on else "", log, False, "queue",
                                tuple(c.path.resolve() for c in camps)))
            continue
        ledger = (cwd / (o.get("ledger") or "tdvms_ledger.jsonl")).resolve()
        runs.append(LiveRun(int(d.name), ledger, o.get("address") or None, slots,
                            o.get("out-dir") or str(campaign_for(ledger).raw), log,
                            "dry-run" in o))
    return runs


def live_for(campaign, runs):
    path = campaign.path.resolve()
    return next((r for r in runs if r.ledger == path and not r.dry_run), None)


def queued_for(campaign, runs):
    """`(run, position)` if a live queue will get to this campaign later."""
    path = campaign.path.resolve()
    for r in runs:
        if path in r.queue and r.ledger != path:
            later = list(r.queue)
            if r.ledger in later and later.index(path) > later.index(r.ledger):
                return r, later.index(path) - later.index(r.ledger)
    return None


# --- what a campaign looks like -----------------------------------------------

def counts(rows):
    return Counter(r["state"] for r in rows)


def progress(rows):
    """Share of chunks that have reached an outcome, 0..1."""
    if not rows:
        return 0.0
    return sum(1 for r in rows if r["state"] in TERMINAL) / len(rows)


def infer_pool(rows):
    """`(base address, slots)` from the plus-addresses a ledger has used.

    For a campaign with no live run to ask. The count is the highest slot seen,
    which can undercount a pool whose top slots never got work -- good enough
    for a table, never used to submit.
    """
    seen = Counter()
    top = {}
    for r in rows:
        m = SLOT_RE.match(r.get("email") or "")
        if m:
            base = f"{m['local']}@{m['domain']}".lower()
            seen[base] += 1
            top[base] = max(top.get(base, 0), int(m["n"]))
    if not seen:
        return None
    base = seen.most_common(1)[0][0]
    return base, top[base]


def pool_for(campaign, run, rows):
    """The slot table's pool: the live run's own, or the ledger's best guess."""
    if run and run.address:
        try:
            return Pool(run.address, run.slots)
        except ValueError:
            return None
    guess = infer_pool(rows)
    return Pool(*guess) if guess else None


def overview(campaign, pool=None):
    lines = []
    summarise(campaign.ledger, pool, out=lines.append)
    return lines


def reset(campaign, cid):
    """`tdvms reset`, by exact chunk id -- the only way the TUI names a chunk."""
    return campaign.ledger.release(cid, note="reset by hand (tdvms-tui)")


# --- planning -----------------------------------------------------------------

def windows(start, end, chunk_days):
    """The `(start, end)` pairs `plan` would enumerate, as ISO strings."""
    t, stop, out = datetime.fromisoformat(start), datetime.fromisoformat(end), []
    if chunk_days < 1:
        raise ValueError("a window is at least one day")
    while t < stop:
        nxt = min(t + timedelta(days=chunk_days), stop)
        out.append((t.isoformat(), nxt.isoformat()))
        t = nxt
    return out


def preview(rows, codes, start, end, chunk_days):
    """`(total, new)` chunks a plan would touch. Nothing is written."""
    have = {chunk_id(r["station"], r["start"]) for r in rows}
    spans = windows(start, end, chunk_days)
    total = len(codes) * len(spans)
    new = sum(1 for c in codes for s, _ in spans if chunk_id(c, s) not in have)
    return total, new


def apply_plan(campaign, codes, start, end, chunk_days):
    """Enumerates the windows into the ledger exactly as `tdvms plan` does."""
    return sum(enumerate_chunks(campaign.ledger, c, start, end, chunk_days)
               for c in codes)


def add_months(d, n):
    """`d` moved by `n` calendar months, the day clamped to the month's end."""
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    last = (date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last))


def suggest_name(event):
    """`"27 km NNE of Van, Turkey"`, 2011 -> `"van-2011"`."""
    import unicodedata
    place = event.place.split(" of ", 1)[-1].split(",")[0].split(" earthquake")[0]
    place = place.translate(str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU"))
    place = unicodedata.normalize("NFKD", place).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", place.lower()).strip("-")[:28] or "quake"
    return f"{slug}-{event.date[:4]}"


def run_command(campaign, root, address=None, slots=None):
    """The unattended `tdvms run` line for a campaign, paths relative to `root`.

    Detached and logging to the campaign's own `run.log`, which is where the
    Log tab looks when no live run says otherwise.
    """
    def rel(p):
        try:
            return os.path.relpath(p, root)
        except ValueError:
            return str(p)
    return (f"setsid nohup uv run tdvms run --ledger {rel(campaign.path)} "
            f"--out-dir {rel(campaign.raw)} "
            f"--address {address or 'you@gmail.com'} --slots {slots or 8} "
            f">> {rel(campaign.log)} 2>&1 &")


# --- strays -------------------------------------------------------------------

class RepairError(Exception):
    pass


@dataclass
class Stray:
    """An `UNMATCHED_*.zip` a run kept because its own ledger lacked the window."""
    path: pathlib.Path
    station: str | None
    start: datetime | None
    campaign: Campaign | None
    row: dict | None

    @property
    def status(self):
        if self.row is None:
            return "orphan"
        if self.row["state"] == "fetched":
            return "duplicate"
        return "file"


def _owner(campaigns, station, start):
    """The campaign whose ledger holds this window, preferring one still open."""
    if station is None:
        return None, None
    want = start.strftime("%Y-%m-%d")
    best = (None, None)
    for c in campaigns:
        for r in c.ledger.rows():
            if r["station"] == station and r["start"][:10] == want:
                if r["state"] not in TERMINAL:
                    return c, r
                if best[1] is None or r["state"] == "fetched":
                    best = (c, r)
    return best


def strays(campaigns):
    out, seen = [], set()
    for c in campaigns:
        if not c.raw.is_dir():
            continue
        for p in sorted(c.raw.glob("UNMATCHED_*.zip")):
            if p.resolve() in seen:
                continue
            seen.add(p.resolve())
            member = p.name[len("UNMATCHED_"):-len(".zip")]
            station, start, _ = fetching.window_from_member(member)
            owner, row = _owner(campaigns, station, start)
            out.append(Stray(p, station, start, owner, row))
    return out


def file_stray(stray):
    """Files a stray against the ledger that holds its window -- `ingest`, plus
    the copy `ingest` cannot do.

    `file_archive` moves with `rename`, which fails across filesystems, and the
    raw folders routinely sit on different disks. So a stray on another disk is
    copied next to its destination first, and the original is removed only once
    the ledger records the fetch.
    """
    if stray.status != "file":
        raise RepairError(f"{stray.path.name} is a {stray.status}; nothing to file")
    c, row, src = stray.campaign, stray.row, stray.path
    c.raw.mkdir(parents=True, exist_ok=True)
    work = src
    if os.stat(src).st_dev != os.stat(c.raw).st_dev:
        work = c.raw / f"recovered.{src.name}"
        shutil.copy2(src, work)
        if work.stat().st_size != src.stat().st_size:
            work.unlink(missing_ok=True)
            raise RepairError(f"copy of {src.name} came out the wrong size")

    members, size, problem = fetching.inspect(work)
    if problem is not None:
        if work != src:
            work.unlink(missing_ok=True)
        raise RepairError(problem.detail)
    cid = chunk_id(row["station"], row["start"])
    if not any(n.lower().endswith(".mseed") for n in members):
        import zipfile
        with zipfile.ZipFile(work) as zf:
            notice = zf.read(members[0])[:200].decode("utf-8", errors="replace").strip()
        c.ledger.update(cid, state="nodata", fetched_at=now(), bytes=size, note=notice)
        work.unlink(missing_ok=True)
        src.unlink(missing_ok=True)
        return f"{cid} recorded as nodata"
    filed = fetching.file_archive(work, row, c.raw, members, size)
    if not isinstance(filed, fetching.Fetched):
        raise RepairError(filed.detail)
    c.ledger.update(cid, state="fetched", fetched_at=now(), bytes=size,
                    note=f"{len(members)} file(s), recovered from {src.name}")
    src.unlink(missing_ok=True)
    return f"{cid} filed -> {filed.path}"


def delete_stray(stray):
    """Removes a stray whose window its ledger already has. Nothing else."""
    if stray.status != "duplicate":
        raise RepairError(f"{stray.path.name} is not a duplicate; refusing to delete")
    stray.path.unlink()


def in_flight(rows):
    return [r for r in rows if r["state"] in IN_FLIGHT]


def tail(path, offset=None, keep=64 * 1024):
    """New text in a growing file since `offset`: `(text, new offset)`.

    From the last `keep` bytes when `offset` is None, and from the start again
    when the file has shrunk (rotated or truncated).
    """
    try:
        size = path.stat().st_size
    except (OSError, AttributeError):
        return "", None
    mid_line = False
    if offset is None or size < offset:
        offset = max(0, size - keep)
        mid_line = offset > 0
    if size == offset:
        return "", offset
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read(size - offset)
    if mid_line:
        data = data.partition(b"\n")[2]
    return data.decode("utf-8", errors="replace"), size
