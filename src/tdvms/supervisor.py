"""One cycle of the campaign: reap, reclaim, retire, submit. In one process.

Not a runnable script -- `run.py` drives it.

This is the whole point of the rewrite. The tool it replaces spread these four
steps over three programs that spoke to each other through a file and a
subprocess call, and every half-state the campaign produced lived in the gaps:
a chunk marked `nodata` whose slot was never refilled, a row left `claimed`
against a request nobody made, a `reset` that landed on two other stations.
Here the four steps run in order, in one process, under one lock, and each is
written so that a crash anywhere leaves work the next cycle can pick up.

**The ordering is not arbitrary.**

1. `reap` first, because an arriving answer frees a slot, and a slot freed now
   can be filled in step 4 of this same cycle instead of waiting a full tick.
2. `reclaim` second, so a slot stuck since before this process started is
   released before anything is submitted, not after.
3. `retire` third, so a dead station stops consuming the slots that step 4 is
   about to hand out.
4. `submit` last, once the free list is as long as it is going to get.
"""
from tdvms import client as portal
from tdvms import fetch as fetching
from tdvms import mailbox as mail
from tdvms.ledger import IN_FLIGHT, age_seconds, chunk_id, now


class Config:
    """Everything the loop is allowed to decide for itself."""

    def __init__(self, out_dir="afad_raw", claim_timeout=600, submit_timeout=21600,
                 max_attempts=3, nodata_streak=8, busy_cooldown=1800,
                 retire=True, stations=None, claim_unknown=False, dry_run=False):
        self.out_dir = out_dir
        # 10 minutes: a submission that has not been answered in that long did
        # not die politely. The portal's own slow path is the ~60 s cutoff.
        self.claim_timeout = claim_timeout
        # 6 hours, about 4x the median turnaround measured over this campaign.
        # Short enough that a lost link does not cost a day, long enough that a
        # merely slow one is not re-requested while it is still coming.
        self.submit_timeout = submit_timeout
        self.max_attempts = max_attempts
        self.nodata_streak = nodata_streak
        self.busy_cooldown = busy_cooldown
        self.retire = retire
        self.stations = stations
        self.claim_unknown = claim_unknown
        self.dry_run = dry_run


class Cycle:
    """What one pass did. Printed by `run`, asserted by the tests."""

    def __init__(self):
        self.fetched, self.nodata, self.requeued = [], [], []
        self.reclaimed, self.retired, self.submitted = [], [], []
        self.busy, self.rejected, self.foreign, self.notes = [], [], [], []

    @property
    def quiet(self):
        return not any((self.fetched, self.nodata, self.requeued, self.reclaimed,
                        self.retired, self.submitted, self.busy, self.rejected))

    def summary(self):
        bits = []
        for name in ("fetched", "nodata", "requeued", "reclaimed", "retired",
                     "submitted", "busy", "rejected"):
            got = getattr(self, name)
            if got:
                bits.append(f"{name} {len(got)}")
        return ", ".join(bits) if bits else "nothing to do"


def cycle(ledger, pool, cfg, mailbox=None, tdvms=None, log=print):
    """Reap, reclaim, retire, submit -- once.

    Args:
        ledger: `ledger.Ledger`.
        pool: `slots.Pool`.
        cfg: `Config`.
        mailbox: anything with `.read(addresses, claim_unknown)` returning
            `(events, connection)` and a `.consume(conn, uid, ok)`. The tests
            pass a fake; that is the reason this is an argument at all.
        tdvms: anything with `.submit(station, start, end, email)` returning a
            `client.Outcome`.

    Returns:
        Cycle.
    """
    out = Cycle()
    pool.refresh(ledger)
    if mailbox is not None:
        _reap(ledger, pool, cfg, mailbox, out, log)
        pool.refresh(ledger)
    _reclaim(ledger, cfg, out, log)
    if cfg.retire:
        _retire(ledger, cfg, out, log)
    pool.refresh(ledger)
    if tdvms is not None:
        _submit(ledger, pool, cfg, tdvms, out, log)
    return out


# --- 1. reap ---------------------------------------------------------------

def _reap(ledger, pool, cfg, mailbox, out, log):
    """Reads the inbox and applies every answer, freeing slots as it goes."""
    try:
        events, conn = mailbox.read(pool.addresses(), cfg.claim_unknown)
    except Exception as e:
        # A dropped IMAP connection is routine on a loop that runs for days.
        # Failing the cycle over it would stop submissions too, which is the
        # opposite of what a transient mail problem calls for.
        out.notes.append(f"mailbox unreadable this pass ({type(e).__name__}: {e})")
        log(f"  mailbox: {type(e).__name__}: {e} — retrying next cycle")
        return
    try:
        for event in events:
            if isinstance(event, mail.Foreign):
                out.foreign.append(event.to)
                continue          # left unread, on purpose; see mailbox.Foreign
            if isinstance(event, mail.Unreadable):
                continue
            if isinstance(event, mail.NoData):
                ok = _handle_nodata(ledger, pool, event, out, log)
            elif isinstance(event, mail.Link):
                ok = _handle_links(ledger, pool, cfg, event, out, log)
            else:
                ok = True         # portal mail we have no use for; consume it
            if cfg.dry_run:
                continue
            try:
                mailbox.consume(conn, event.uid, ok)
            except Exception as e:
                # The connection died while we were busy, and an 884 MB fetch
                # takes long enough for a mail server to drop an idle session --
                # Gmail does, and it killed the whole loop right after filing
                # the archive. The ledger write already happened, so nothing is
                # lost; the message stays unread and is re-read next cycle,
                # where the already-banked check retires it without
                # re-downloading. Abandon the rest of THIS pass, because every
                # further consume on a dead socket raises the same way.
                out.notes.append(f"mail connection dropped mid-pass "
                                 f"({type(e).__name__})")
                log(f"  mail connection dropped after the download "
                    f"({type(e).__name__}) — the ledger is already written; "
                    f"the message stays unread and is skipped next cycle")
                break
    except Exception as e:
        # Same reasoning one level up: a mail problem must not stop submissions.
        out.notes.append(f"reap failed ({type(e).__name__}: {e})")
        log(f"  reap: {type(e).__name__}: {e} — carrying on to the queue")
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _handle_nodata(ledger, pool, event, out, log):
    """The portal answered with no link. The address names the window.

    Retiring the chunk and freeing the slot happen in one locked write. Doing
    them as two subprocess calls is what drained a queue to zero: five of these
    arrived, five chunks were retired, and not one refill fired.
    """
    slot = pool.get(event.to)
    row = slot.holding if slot else ledger.held_by(event.to)
    if row is None:
        log(f"  no-data mail to {event.to}, which holds nothing — nothing recorded")
        return True
    cid = chunk_id(row["station"], row["start"])
    ledger.update(cid, state="nodata", fetched_at=now(),
                  note="portal: veri bulunmamaktadir (mail, no link)")
    if slot:
        slot.holding = None
    out.nodata.append(cid)
    log(f"  [NODATA] {cid} — no waveform at source; slot {event.to} free")
    return True


def _handle_links(ledger, pool, cfg, event, out, log):
    """Downloads each link, verifies it, and files it against its own window."""
    clean = True
    for url in event.urls:
        if any(r.get("url") == url and r["state"] == "fetched" for r in ledger.rows()):
            log(f"  [SKIP] already banked: {url.rsplit('/', 1)[-1]}")
            continue
        if cfg.dry_run:
            log(f"  DRY would fetch {url.rsplit('/', 1)[-1]} for {event.to}")
            continue
        result = _ingest(ledger, pool, cfg, url, event.to, out, log)
        if not result.ok:
            clean = False
    return clean


def _ingest(ledger, pool, cfg, url, to, out, log):
    """One link, start to finish: download, inspect, match, record, free."""
    try:
        path, failure = fetching.download(url, cfg.out_dir)
    except Exception as e:
        log(f"  [FAIL] {type(e).__name__}: {e}")
        return fetching.Expired(str(e))
    if failure is not None:
        log(f"  [EXPIRED] {failure.detail}")
        _requeue_slot(ledger, pool, to, out, log, "link expired")
        return failure

    members, size, problem = fetching.inspect(path)
    if problem is not None:
        path.unlink(missing_ok=True)
        if isinstance(problem, fetching.EmptyArchive):
            # Transient: the portal failed to build the archive, the window
            # still holds data. Requeue rather than retire.
            log(f"  [EMPTY] {problem.detail} — requeuing the window")
            _requeue_slot(ledger, pool, to, out, log, "empty archive, requeued")
        else:
            log(f"  [BAD] {problem.detail}")
            _requeue_slot(ledger, pool, to, out, log, problem.detail)
        return problem

    station, start, _ = fetching.window_from_member(members[0])
    row = None
    if start is not None:
        want = start.strftime("%Y-%m-%d")
        row = next((r for r in ledger.rows()
                    if r["station"] == station and r["start"][:10] == want), None)

    if not any(n.lower().endswith(".mseed") for n in members):
        import zipfile
        with zipfile.ZipFile(path) as zf:
            notice = zf.read(members[0])[:200].decode("utf-8", errors="replace").strip()
        path.unlink(missing_ok=True)
        if row is None:
            # The member name did not parse, but the address still names the
            # window: a slot holds exactly one chunk at a time. Falling back to
            # it is the difference between recording a no-data answer and
            # waiting out the submit timeout for a link that is never coming.
            slot = pool.get(to)
            row = slot.holding if slot else ledger.held_by(to)
        if row is None:
            log("  [NODATA] notice for a window this ledger does not hold")
            return fetching.NoDataNotice(notice, station, start, size)
        cid = chunk_id(row["station"], row["start"])
        ledger.update(cid, state="nodata", fetched_at=now(), url=url,
                      bytes=size, note=notice)
        _free(pool, to)
        out.nodata.append(cid)
        log(f"  [NODATA] {cid} — {notice[:70]}")
        return fetching.NoDataNotice(notice, station, start, size)

    if row is None:
        stray = path.parent / f"UNMATCHED_{members[0].rsplit('/', 1)[-1]}.zip"
        path.replace(stray)
        log(f"  [UNMATCHED] '{members[0]}' matches no chunk — kept as {stray.name}")
        return fetching.Unmatched(stray, members[0])

    filed = fetching.file_archive(path, row, cfg.out_dir, members, size)
    cid = chunk_id(row["station"], row["start"])
    if isinstance(filed, fetching.Fetched):
        ledger.update(cid, state="fetched", fetched_at=now(), url=url, bytes=size,
                      note=f"{len(members)} file(s)")
        _free(pool, to)
        out.fetched.append(cid)
        log(f"  [OK] {cid} — {filed.detail} -> {filed.path}")
    else:
        log(f"  [WARN] {filed.detail}")
    return filed


def _free(pool, email):
    slot = pool.get(email)
    if slot:
        slot.holding = None


def _requeue_slot(ledger, pool, email, out, log, note):
    """Puts whatever this address holds back to pending and frees the slot."""
    slot = pool.get(email)
    row = slot.holding if slot else ledger.held_by(email)
    if row is None:
        return
    cid = chunk_id(row["station"], row["start"])
    attempts = (row.get("attempts") or 0) + 1
    ledger.release(cid, note=note)
    ledger.update(cid, attempts=attempts)
    _free(pool, email)
    out.requeued.append(cid)


# --- 2. reclaim ------------------------------------------------------------

def _reclaim(ledger, cfg, out, log):
    """Releases slots held against requests that are never going to answer.

    Two stalls, two timeouts. A `claimed` row is a submission that died between
    taking the chunk and hearing back, which is seconds of work -- ten minutes
    is already generous. A `submitted` row is waiting on the portal's queue,
    which genuinely takes hours.

    Past `--max-attempts` the chunk is retired rather than requeued. A window
    that has failed three times is not going to succeed on the fourth, and a
    chunk that requeues forever is a slot that is never available for work that
    could succeed.
    """
    for row in ledger.in_state(*IN_FLIGHT):
        cid = chunk_id(row["station"], row["start"])
        if row["state"] == "claimed":
            stamp, limit, why = row.get("claimed_at"), cfg.claim_timeout, "died mid-POST"
        else:
            stamp, limit, why = row.get("submitted_at"), cfg.submit_timeout, "link never arrived"
        age = age_seconds(stamp)
        if age is None:
            # No stamp at all -- a row adopted from the old ledger, or written
            # before the stamps existed. Give it one, so the NEXT cycle can time
            # it out honestly rather than reaping it on no evidence.
            ledger.update(cid, **{"claimed_at" if row["state"] == "claimed"
                                  else "submitted_at": now()})
            log(f"  [STAMP] {cid} had no {row['state']}_at; stamped now")
            continue
        if age < limit:
            continue
        attempts = (row.get("attempts") or 0) + 1
        if attempts > cfg.max_attempts:
            ledger.update(cid, state="failed", email=None, attempts=attempts,
                          note=f"{why}; gave up after {attempts - 1} attempt(s)")
            out.rejected.append(cid)
            log(f"  [GIVE UP] {cid} — {why}, {attempts - 1} attempts")
            continue
        ledger.release(cid, note=f"reclaimed after {age/60:.0f} min ({why})")
        ledger.update(cid, attempts=attempts)
        out.reclaimed.append(cid)
        log(f"  [RECLAIM] {cid} — {why} after {age/60:.0f} min; attempt {attempts}")


def _retire_station(ledger, station, why, out, log):
    """Retires every pending chunk for a station, once and immediately.

    A permanent rejection is a fact about the STATION, not about the window. A
    station the portal does not list will not be listed on the next window
    either, so there is nothing to learn by asking again -- and asking again is
    expensive: a ledger planned as `TU.KAND` rather than `KAND` submitted and
    failed all 40 of its windows, one per free slot per cycle, because the only
    retirement rule counted no-data answers. One rejection is enough.
    """
    doomed = [r for r in ledger.rows()
              if r["station"] == station and r["state"] == "pending"]
    if not doomed:
        return
    for row in doomed:
        cid = chunk_id(row["station"], row["start"])
        ledger.update(cid, state="retired", email=None,
                      note=f"station retired after a permanent rejection: {why}")
        out.retired.append(cid)
    log(f"  [RETIRE] {station}: {why}\n"
        f"           {len(doomed)} pending chunk(s) retired — the next window "
        f"would fail the same way")


# --- 3. retire -------------------------------------------------------------

def _retire(ledger, cfg, out, log):
    """Stops submitting for a station that has never returned anything.

    BLKS answered `nodata` 26 times out of 26 and fetched nothing, ever. Each
    of those answers freed a slot that was immediately refilled with another
    BLKS window, so the campaign spent roughly 30 submissions establishing the
    same fact 30 times. The rule needs both halves: consecutive no-data answers
    AND nothing ever fetched. A station with a genuine gap in the middle of a
    good span trips the first and not the second, and must not be retired.
    """
    for station in ledger.stations():
        rows = [r for r in ledger.rows() if r["station"] == station]
        if any(r["state"] == "fetched" for r in rows):
            continue
        nodata = sum(1 for r in rows if r["state"] == "nodata")
        if nodata < cfg.nodata_streak:
            continue
        dead = [r for r in rows if r["state"] == "pending"]
        if not dead:
            continue
        for row in dead:
            cid = chunk_id(row["station"], row["start"])
            ledger.update(cid, state="retired", email=None,
                          note=f"station retired: {nodata} no-data answers, 0 fetched")
            out.retired.append(cid)
        log(f"  [RETIRE] {station}: {nodata} no-data, 0 fetched — "
            f"{len(dead)} pending chunk(s) retired, slots released")


# --- 4. submit -------------------------------------------------------------

def _submit(ledger, pool, cfg, tdvms, out, log):
    """Fills every genuinely free slot, oldest window first, across stations.

    Round-robin over stations rather than strict global date order: with one
    station holding 36 pending windows and another 3, strict order gives the
    first station every slot for days and the campaign learns nothing about the
    second until it is finished with the first.
    """
    from datetime import datetime as dt

    free = pool.free()
    if not free:
        return
    for slot in free:
        allowed = _next_stations(ledger, cfg, out)
        row, blocker = ledger.claim(slot.email, stations=allowed)
        if row is None:
            if blocker is None:
                log("  nothing pending — campaign complete")
                return
            continue
        cid = chunk_id(row["station"], row["start"])
        if cfg.dry_run:
            ledger.release(cid)
            log(f"  DRY would submit {cid} -> {slot.email}")
            continue
        log(f"  submitting {cid} -> {slot.email}")
        result = tdvms.submit(row["station"], dt.fromisoformat(row["start"]),
                              dt.fromisoformat(row["end"]), slot.email)
        _record(ledger, pool, slot, cid, row, result, cfg, out, log)


def _next_stations(ledger, cfg, out):
    """Which stations this slot may draw from, so one cannot starve the rest.

    Ordered by how few chunks are already in flight for them. `None` means no
    restriction, which is what happens once every station has equal footing.
    """
    if cfg.stations:
        return cfg.stations
    pending = {}
    inflight = {}
    for r in ledger.rows():
        if r["state"] == "pending":
            pending[r["station"]] = pending.get(r["station"], 0) + 1
        elif r["state"] in IN_FLIGHT:
            inflight[r["station"]] = inflight.get(r["station"], 0) + 1
    if not pending:
        return None
    fewest = min(inflight.get(s, 0) for s in pending)
    return [s for s in pending if inflight.get(s, 0) == fewest]


def _record(ledger, pool, slot, cid, row, result, cfg, out, log):
    """Writes the portal's answer to the ledger and to the slot's remote state."""
    if isinstance(result, portal.Busy):
        # Local said free, the portal says otherwise -- and the portal is the
        # authority. Back the chunk out and cool the slot down, or the next
        # cycle submits into the same 111.
        ledger.release(cid, note="portal busy; local state said this slot was free")
        slot.mark_busy(cfg.busy_cooldown)
        out.busy.append(slot.email)
        log(f"  [BUSY] {slot.email} — the portal still holds its last request; "
            f"cooling down {cfg.busy_cooldown // 60} min")
        return
    if isinstance(result, portal.Rejected):
        attempts = (row.get("attempts") or 0) + 1
        if result.permanent or attempts > cfg.max_attempts:
            ledger.update(cid, state="failed", email=None, attempts=attempts,
                          note=result.detail)
            out.rejected.append(cid)
            log(f"  [FAILED] {cid} — {result.detail}")
            if result.permanent:
                _retire_station(ledger, row["station"], result.detail, out, log)
        else:
            ledger.release(cid, note=result.detail)
            ledger.update(cid, attempts=attempts)
            out.requeued.append(cid)
            log(f"  [RETRY] {cid} — {result.detail} (attempt {attempts})")
        slot.holding = None
        slot.last_result = "rejected"
        return
    note = result.detail if isinstance(result, portal.AcceptedUnconfirmed) else None
    ledger.update(cid, state="submitted", submitted_at=now(), note=note)
    slot.holding = ledger.by_id(cid)
    slot.last_result = "accepted"
    out.submitted.append(cid)
    if isinstance(result, portal.AcceptedUnconfirmed):
        log(f"  [UNCONFIRMED] {cid} — {result.detail}; kept as submitted")
    else:
        log(f"  [SUBMITTED] {cid} -> {slot.email}")
