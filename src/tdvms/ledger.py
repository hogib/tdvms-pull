"""The campaign's only state: one JSON line per (station, window) chunk.

Not a runnable script -- imported only.

**A chunk is identified by `station:start`, never by date.** The tool this
replaces matched `reset` with `r["start"].startswith(args.start)`, across every
station in the file. `reset --start 2024-09-17` was meant for one ELBA window
and hit three rows in two stations, two of which were already `nodata`; the
slot spent re-requesting them was simply gone. There is no date-keyed path in
here, so that cannot be written again by accident.

**Every write is a read-modify-write under one flock.** `fetch` holds its view
of the ledger for the several minutes a 800 MB download takes, and writing that
stale copy back silently discarded whatever a submission recorded meanwhile.
That happened: a submission was erased, its window returned to pending, and a
second address was handed the same window -- a duplicate costing a queue slot
that TDVMS then refused to give back.

**States.**

    pending -> claimed -> submitted -> fetched
                                    -> nodata      (TDVMS has no waveform)
                                    -> failed      (retries exhausted)
               claimed -> pending                  (reaped: died mid-POST)
               submitted -> pending                (reaped: link never came)
    pending -> retired                             (its station is dead)

`claimed` is the narrow window between taking a chunk and hearing back from the
portal. It holds the address's slot, because a submission mid-POST occupies
that slot exactly as much as a confirmed one -- and it carries `claimed_at` so
a process that dies inside that window leaves something the next cycle can see.
Without the stamp such a row sat forever: SLV held a slot for a day and a half
looking indistinguishable from mail that was merely slow.
"""
import contextlib
import fcntl
import json
import pathlib
from datetime import datetime, timedelta, timezone

# In flight at the portal, and therefore holding an address's queue slot.
IN_FLIGHT = ("claimed", "submitted")
# Reached an outcome; no slot, no further work.
TERMINAL = ("fetched", "nodata", "failed", "retired")
STATES = ("pending",) + IN_FLIGHT + TERMINAL


def now():
    """UTC, seconds resolution -- the stamp format every row in the ledger uses."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def chunk_id(station, start):
    """`"ELBA:2025-09-09T00:00:00"` -- what every mutation is keyed by."""
    return f"{station}:{start}"


def age_seconds(stamp, reference=None):
    """Seconds since an ISO stamp, or None if it is missing or unparseable.

    None rather than 0: a row with no stamp is a row we know nothing about, and
    reporting that as "0 seconds old" would make the reaper treat the oldest
    stall in the file as the freshest thing in it.
    """
    if not stamp:
        return None
    try:
        t = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    ref = reference or datetime.now(timezone.utc)
    return (ref - t).total_seconds()


class Ledger:
    """The chunk table, addressed by `chunk_id`, serialised by an flock.

    Reads are cheap and uncached on purpose. Several processes may hold the
    same ledger -- a `run` loop and an operator typing `status` -- and a cache
    would let one of them act on a picture the other has already invalidated.
    """

    def __init__(self, path):
        self.path = pathlib.Path(path)

    # --- raw io ------------------------------------------------------------

    def rows(self):
        """Every chunk, in file order, as plain dicts."""
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]

    def _write(self, rows):
        """Atomic replace: a crash mid-write must not truncate the campaign.

        Writing in place left a half-line on a killed process once, and
        `load` then refused the whole file -- 137 chunks of recorded work
        unreadable because of one interrupted `save`.
        """
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
        tmp.replace(self.path)

    @contextlib.contextmanager
    def locked(self):
        """Exclusive access for the duration of a read-modify-write.

        The lock file is separate from the ledger so the atomic replace above
        cannot pull the inode out from under a waiter.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # --- queries (no lock; callers that mutate take one) --------------------

    def by_id(self, cid):
        return next((r for r in self.rows() if chunk_id(r["station"], r["start"]) == cid),
                    None)

    def in_state(self, *states):
        return [r for r in self.rows() if r["state"] in states]

    def held_by(self, email):
        """The chunk this address currently occupies the portal with, if any."""
        e = (email or "").strip().lower()
        return next((r for r in self.rows()
                     if r["state"] in IN_FLIGHT and (r.get("email") or "").lower() == e),
                    None)

    def stations(self):
        seen = []
        for r in self.rows():
            if r["station"] not in seen:
                seen.append(r["station"])
        return seen

    # --- mutation ----------------------------------------------------------

    def update(self, cid, **changes):
        """Applies changes to one chunk against the CURRENT on-disk ledger.

        Never writes back a view loaded earlier. That is the lost-write above,
        and it is why this takes a `chunk_id` rather than a row object: a row
        handed in from elsewhere is by definition a stale copy.
        """
        with self.locked():
            rows = self.rows()
            for r in rows:
                if chunk_id(r["station"], r["start"]) == cid:
                    r.update(changes)
                    self._write(rows)
                    return r
        raise KeyError(f"{cid} is not in {self.path}")

    def add(self, station, start, end):
        """Appends one pending chunk. Caller holds the lock."""
        return {"station": station, "start": start, "end": end, "state": "pending",
                "email": None, "url": None, "bytes": None, "note": None,
                "attempts": 0, "claimed_at": None, "submitted_at": None,
                "fetched_at": None}

    def claim(self, email, stations=None):
        """Takes the OLDEST pending chunk for `email` and marks it claimed.

        Returns:
            (row, blocker). `blocker` is the chunk this address already holds,
            in which case nothing was claimed.

        Oldest by start date, not by file order. The tool this replaces took
        `next(r for r in rows if r["state"] == "pending")` -- first in the
        FILE -- so a station appended later jumped ahead of windows that had
        been waiting since the campaign began, for no reason anyone could see
        from the outside.
        """
        with self.locked():
            rows = self.rows()
            e = email.strip().lower()
            held = next((r for r in rows if r["state"] in IN_FLIGHT
                         and (r.get("email") or "").lower() == e), None)
            if held:
                return None, held
            todo = [r for r in rows if r["state"] == "pending"
                    and (stations is None or r["station"] in stations)]
            if not todo:
                return None, None
            row = min(todo, key=lambda r: (r["start"], r["station"]))
            row["state"], row["email"], row["claimed_at"] = "claimed", email, now()
            self._write(rows)
            return dict(row), None

    def release(self, cid, note=None):
        """Back to pending, and every trace of the attempt cleared.

        The stamps go too. Leaving a stale `fetched_at` on a requeued row made
        `status` compute a turnaround from a fetch that happened before the
        submission it was subtracted from -- `range -3064-76 min`, which is not
        a slow request but arithmetic on two unrelated events.
        """
        return self.update(cid, state="pending", email=None, note=note,
                           claimed_at=None, submitted_at=None, fetched_at=None,
                           url=None, bytes=None)


def load(path):
    return Ledger(path)
