"""The loop, as a pure function of a ledger, an inbox and a portal.

Every test here is a failure this campaign actually produced while being driven
by three programs that handed state to each other through a file. The point of
the rewrite is that they are now expressible as one assertion about one process.
"""
import pytest

from conftest import (Accepted, AcceptedUnconfirmed, Busy, FakeMailbox,
                      FakePortal, Rejected, chunk, message, write)
from tdvms.ledger import now
from tdvms.mailbox import Foreign, Link, NoData
from tdvms.slots import Pool
from tdvms.supervisor import Config, cycle


def quiet(*_a, **_k):
    pass


@pytest.fixture
def cfg(tmp_path):
    return Config(out_dir=str(tmp_path / "raw"))


# --- the slot that was freed and never refilled ----------------------------

def test_a_nodata_answer_frees_its_slot_and_the_slot_is_refilled(ledger_path, cfg):
    """The GCAM drain: five no-data mails arrived, five chunks were retired,
    and not one refill fired, because marking and refilling were two separate
    subprocess calls with nothing owning the pair. Six addresses then sat idle
    holding nothing while the queue was empty."""
    led = write(ledger_path, [
        chunk("ELBA", "2025-09-09T00:00:00", state="submitted",
              email="you+a1@gmail.com", submitted_at=now()),
        chunk("ELBA", "2025-10-01T00:00:00"),
    ])
    pool = Pool("you@gmail.com", 1)
    box = FakeMailbox(NoData(b"1", "you+a1@gmail.com"))
    portal = FakePortal(Accepted(0, "ok"))

    out = cycle(led, pool, cfg, box, portal, log=quiet)

    assert out.nodata == ["ELBA:2025-09-09T00:00:00"]
    assert out.submitted == ["ELBA:2025-10-01T00:00:00"], (
        "the slot the no-data answer freed was not refilled in the same cycle")
    assert portal.calls[0][2] == "you+a1@gmail.com"


# --- the slot held against a request nobody made ---------------------------

def test_a_claim_that_died_mid_post_is_reclaimed(ledger_path, cfg):
    """SLV sat `claimed` for a day and a half. From outside it was
    indistinguishable from mail that had not arrived yet, and the slot was
    never available to anything."""
    led = write(ledger_path, [chunk("SLV", state="claimed", email="you+a1@gmail.com",
                                    claimed_at="2020-01-01T00:00:00+00:00")])
    cfg.claim_timeout = 600
    out = cycle(led, Pool("you@gmail.com", 1), cfg, None, None, log=quiet)
    assert out.reclaimed == ["SLV:2025-09-09T00:00:00"]
    row = led.by_id("SLV:2025-09-09T00:00:00")
    assert row["state"] == "pending" and row["email"] is None
    assert row["attempts"] == 1


def test_a_submission_still_inside_its_window_is_left_alone(ledger_path, cfg):
    """Reaping a merely slow request is worse than waiting: it hands the same
    window to a second address and the portal refuses the duplicate."""
    led = write(ledger_path, [chunk("ELBA", state="submitted",
                                    email="you+a1@gmail.com", submitted_at=now())])
    out = cycle(led, Pool("you@gmail.com", 1), cfg, None, None, log=quiet)
    assert out.reclaimed == []
    assert led.by_id("ELBA:2025-09-09T00:00:00")["state"] == "submitted"


def test_an_unstamped_in_flight_row_is_stamped_not_reaped(ledger_path, cfg):
    """An adopted row has no stamp. Reaping it on no evidence would requeue a
    request that may be minutes from answering."""
    led = write(ledger_path, [chunk("ELBA", state="submitted", email="you+a1@gmail.com")])
    out = cycle(led, Pool("you@gmail.com", 1), cfg, None, None, log=quiet)
    assert out.reclaimed == []
    assert led.by_id("ELBA:2025-09-09T00:00:00")["submitted_at"] is not None


def test_a_chunk_that_keeps_failing_is_retired_rather_than_requeued(ledger_path, cfg):
    """A window that requeues forever is a slot permanently unavailable to work
    that could succeed."""
    led = write(ledger_path, [chunk("ELBA", state="claimed", email="you+a1@gmail.com",
                                    claimed_at="2020-01-01T00:00:00+00:00", attempts=3)])
    cfg.max_attempts = 3
    out = cycle(led, Pool("you@gmail.com", 1), cfg, None, None, log=quiet)
    assert out.rejected == ["ELBA:2025-09-09T00:00:00"]
    assert led.by_id("ELBA:2025-09-09T00:00:00")["state"] == "failed"


# --- local `failed` is not remote free -------------------------------------

def test_a_portal_busy_backs_the_chunk_out_and_cools_the_slot(ledger_path, cfg):
    """BLKS was retired locally and `+m2` still answered `[111] BUSY`: the
    portal was holding the abandoned requests. Local state is not authoritative
    about the portal's queue, and a loop that re-submits into a 111 burns a
    cycle every tick."""
    led = write(ledger_path, [chunk("ELBA"), chunk("MANT")])
    pool = Pool("you@gmail.com", 1)
    portal = FakePortal(Busy("still processing"))
    cfg.busy_cooldown = 1800

    out = cycle(led, pool, cfg, None, portal, log=quiet)

    assert out.busy == ["you+a1@gmail.com"]
    assert led.by_id("ELBA:2025-09-09T00:00:00")["state"] == "pending"
    assert pool.get("you+a1@gmail.com").remote_busy
    assert pool.free() == [], "a cooling slot is not free"


def test_a_cooling_slot_is_not_submitted_into_again(ledger_path, cfg):
    led = write(ledger_path, [chunk("ELBA"), chunk("MANT")])
    pool = Pool("you@gmail.com", 1)
    portal = FakePortal(Busy("still processing"), Accepted(0, "ok"))
    cycle(led, pool, cfg, None, portal, log=quiet)
    cycle(led, pool, cfg, None, portal, log=quiet)
    assert len(portal.calls) == 1, "the second cycle submitted into the cooldown"


# --- the ~60 s cutoff ------------------------------------------------------

def test_an_unconfirmed_submission_keeps_its_claim(ledger_path, cfg):
    """The portal disconnects at ~60 s under load and accepts the request
    anyway. Releasing the claim hands the same window to a second address, and
    the portal refuses the duplicate -- which is what made this look like a
    banned address for an evening."""
    led = write(ledger_path, [chunk("ELBA")])
    led_out = cycle(led, Pool("you@gmail.com", 1), cfg, None,
                    FakePortal(AcceptedUnconfirmed("RemoteDisconnected")), log=quiet)
    row = led.by_id("ELBA:2025-09-09T00:00:00")
    assert row["state"] == "submitted", "an unconfirmed request was thrown away"
    assert row["email"] == "you+a1@gmail.com"
    assert led_out.submitted == ["ELBA:2025-09-09T00:00:00"]


def test_a_station_the_portal_does_not_list_is_not_retried(ledger_path, cfg):
    """BAKC and IRLI stalled two slots for hours. A station missing from the
    portal's own list will not appear on a retry."""
    led = write(ledger_path, [chunk("BAKC")])
    out = cycle(led, Pool("you@gmail.com", 1), cfg, None,
                FakePortal(Rejected("BAKC: not in the TDVMS station list",
                                    permanent=True)), log=quiet)
    assert out.rejected == ["BAKC:2025-09-09T00:00:00"]
    row = led.by_id("BAKC:2025-09-09T00:00:00")
    assert row["state"] == "failed" and row["email"] is None


# --- the dead station ------------------------------------------------------

def test_a_station_that_never_returns_data_stops_taking_slots(ledger_path, cfg):
    """BLKS: 26 no-data answers out of 26, nothing ever fetched, roughly 30
    submissions spent establishing the same fact repeatedly. Each answer freed
    a slot that was immediately refilled with another BLKS window."""
    rows = [chunk("BLKS", f"2025-{m:02d}-01T00:00:00", state="nodata")
            for m in range(1, 9)]
    rows += [chunk("BLKS", "2025-10-01T00:00:00"), chunk("BLKS", "2025-11-01T00:00:00")]
    led = write(ledger_path, rows)
    cfg.nodata_streak = 8

    out = cycle(led, Pool("you@gmail.com", 1), cfg, None, FakePortal(), log=quiet)

    assert len(out.retired) == 2
    assert {r["state"] for r in led.rows() if r["start"].startswith("2025-1")} == {"retired"}
    assert out.submitted == [], "a retired station must not consume a slot"


def test_a_station_with_a_gap_in_a_good_span_is_not_retired(ledger_path, cfg):
    """ELBA has 9 no-data windows and 20 fetched. The rule needs both halves,
    or a real station is abandoned over a genuine gap in its record."""
    rows = [chunk("ELBA", f"2025-{m:02d}-01T00:00:00", state="nodata")
            for m in range(1, 10)]
    rows.append(chunk("ELBA", "2024-05-01T00:00:00", state="fetched"))
    rows.append(chunk("ELBA", "2025-12-01T00:00:00"))
    led = write(ledger_path, rows)
    cfg.nodata_streak = 8

    out = cycle(led, Pool("you@gmail.com", 1), cfg, None, FakePortal(), log=quiet)

    assert out.retired == []
    assert out.submitted == ["ELBA:2025-12-01T00:00:00"]


def test_retirement_can_be_turned_off(ledger_path, cfg):
    rows = [chunk("BLKS", f"2025-{m:02d}-01T00:00:00", state="nodata")
            for m in range(1, 9)] + [chunk("BLKS", "2025-10-01T00:00:00")]
    led = write(ledger_path, rows)
    cfg.retire = False
    out = cycle(led, Pool("you@gmail.com", 1), cfg, None, FakePortal(), log=quiet)
    assert out.retired == []
    assert out.submitted == ["BLKS:2025-10-01T00:00:00"]


# --- one mailbox, several campaigns ----------------------------------------

def test_a_link_for_another_ledger_is_left_unread(ledger_path, cfg):
    """Two of a probe's four requests were consumed by another poller and
    logged as permanent failures, while the ledger that owned them still listed
    them as submitted. Returning it unread is what lets the owner pick it up."""
    led = write(ledger_path, [chunk("ELBA", state="submitted",
                                    email="you+a1@gmail.com", submitted_at=now())])
    box = FakeMailbox(Foreign(b"7", "someone+z9@gmail.com"))
    out = cycle(led, Pool("you@gmail.com", 1), cfg, box, FakePortal(), log=quiet)
    assert out.foreign == ["someone+z9@gmail.com"]
    assert box.consumed == [], "a foreign message must not be marked read"
    assert led.by_id("ELBA:2025-09-09T00:00:00")["state"] == "submitted"


# --- resilience ------------------------------------------------------------

def test_a_dead_mailbox_does_not_stop_submissions(ledger_path, cfg):
    """A dropped IMAP connection is routine on a loop that runs for days.
    Failing the cycle over it would idle the queue for a mail problem."""
    led = write(ledger_path, [chunk("ELBA")])
    box = FakeMailbox(boom=OSError("connection reset"))
    out = cycle(led, Pool("you@gmail.com", 1), cfg, box, FakePortal(), log=quiet)
    assert out.notes and "connection reset" in out.notes[0]
    assert out.submitted == ["ELBA:2025-09-09T00:00:00"]


def test_one_station_cannot_starve_the_others(ledger_path, cfg):
    """Strict global date order gives a station with 36 pending windows every
    slot for days, and the campaign learns nothing about the others until it is
    finished with the first."""
    rows = [chunk("MANT", f"2024-{m:02d}-01T00:00:00") for m in range(1, 7)]
    rows.append(chunk("ELBA", "2025-06-01T00:00:00"))
    led = write(ledger_path, rows)
    portal = FakePortal()
    cycle(led, Pool("you@gmail.com", 2), cfg, None, portal, log=quiet)
    assert {c[0] for c in portal.calls} == {"MANT", "ELBA"}


def test_a_dry_run_changes_nothing(ledger_path, cfg):
    led = write(ledger_path, [chunk("ELBA")])
    cfg.dry_run = True
    before = ledger_path.read_text()
    portal = FakePortal()
    cycle(led, Pool("you@gmail.com", 1), cfg, None, portal, log=quiet)
    assert portal.calls == []
    assert led.by_id("ELBA:2025-09-09T00:00:00")["state"] == "pending"


def test_an_empty_campaign_says_so_rather_than_looping(ledger_path, cfg):
    led = write(ledger_path, [chunk("ELBA", state="fetched")])
    out = cycle(led, Pool("you@gmail.com", 2), cfg, None, FakePortal(), log=quiet)
    assert out.submitted == [] and out.quiet
