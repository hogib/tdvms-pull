"""The three failures of the 2026-09-11 live run, one test each.

All three were mine except the typo, and the typo only cost anything because
the tool let it:

1. A network-qualified station code (`TU.KAND` for `KAND`) reached the portal as
   a name that cannot exist. Every one of its 40 windows was submitted and
   permanently rejected, one per free slot per cycle, because the only
   retirement rule counted no-data answers.
2. An 884 MB fetch took long enough for Gmail to drop the idle IMAP session, and
   marking the message read then raised `IMAP4.abort` from inside the reap loop,
   which was unguarded. The process died. The archive was already filed and the
   ledger already written, so nothing was lost -- but the loop stopped.
3. Nothing validated the station code at plan time, where the fix is one word.
"""
import json

import pytest

from conftest import FakeMailbox, FakePortal, Rejected, chunk, write
from tdvms.ledger import now
from tdvms.mailbox import Link, NoData
from tdvms.plan import normalise, verify
from tdvms.slots import Pool
from tdvms.supervisor import Config, cycle


def quiet(*_a, **_k):
    pass


# --- 1. one rejection retires the station ----------------------------------

def test_a_permanent_rejection_retires_every_pending_window_at_once(ledger_path, tmp_path):
    """40 windows, 40 submissions, 40 identical answers. One is enough: a
    station the portal does not list will not be listed for the next window."""
    rows = [chunk("TU.KAND", f"2024-{m:02d}-01T00:00:00") for m in range(1, 13)]
    led = write(ledger_path, rows)
    portal = FakePortal(Rejected("TU.KAND: not in the TDVMS station list",
                                 permanent=True))

    out = cycle(led, Pool("you@gmail.com", 8), Config(out_dir=str(tmp_path)),
                None, portal, log=quiet)

    assert len(portal.calls) == 1, (
        f"submitted {len(portal.calls)} windows for a station the portal does "
        f"not list")
    assert out.rejected == ["TU.KAND:2024-01-01T00:00:00"]
    assert len(out.retired) == 11
    assert not [r for r in led.rows() if r["state"] == "pending"]


def test_a_retryable_rejection_does_not_retire_the_station(ledger_path, tmp_path):
    """A transient error is about the request, not the station. Retiring on it
    would abandon a station over one bad minute."""
    rows = [chunk("ELBA", f"2024-{m:02d}-01T00:00:00") for m in range(1, 4)]
    led = write(ledger_path, rows)
    portal = FakePortal(Rejected("HTTP 502: gateway"))

    out = cycle(led, Pool("you@gmail.com", 1), Config(out_dir=str(tmp_path)),
                None, portal, log=quiet)

    assert out.retired == []
    assert led.by_id("ELBA:2024-01-01T00:00:00")["state"] == "pending"


def test_a_permanent_rejection_leaves_other_stations_untouched(ledger_path, tmp_path):
    led = write(ledger_path, [chunk("TU.KAND", "2024-01-01T00:00:00"),
                              chunk("TU.KAND", "2024-02-01T00:00:00"),
                              chunk("ELBA", "2024-03-01T00:00:00")])
    portal = FakePortal(Rejected("no such station", permanent=True))
    cycle(led, Pool("you@gmail.com", 2), Config(out_dir=str(tmp_path)),
          None, portal, log=quiet)
    assert led.by_id("ELBA:2024-03-01T00:00:00")["state"] in ("submitted", "pending")
    assert led.by_id("TU.KAND:2024-02-01T00:00:00")["state"] == "retired"


# --- 2. the connection that died mid-pass ----------------------------------

class DyingMailbox(FakeMailbox):
    """Reads fine, then raises on every consume -- Gmail dropping an idle
    session while an 884 MB archive was downloading."""

    def consume(self, conn, uid, ok=True):
        import imaplib
        raise imaplib.IMAP4.abort("socket error: EOF")


def test_a_dropped_mail_connection_does_not_kill_the_cycle(ledger_path, tmp_path):
    """It killed the process on the live run, immediately after filing a
    884 MB archive. The ledger write had already happened."""
    led = write(ledger_path, [
        chunk("ELBA", "2025-09-09T00:00:00", state="submitted",
              email="you+a1@gmail.com", submitted_at=now()),
        chunk("ELBA", "2025-10-01T00:00:00"),
    ])
    box = DyingMailbox(NoData(b"1", "you+a1@gmail.com"))
    out = cycle(led, Pool("you@gmail.com", 1), Config(out_dir=str(tmp_path)),
                box, FakePortal(), log=quiet)

    assert out.nodata == ["ELBA:2025-09-09T00:00:00"], "the answer was recorded"
    assert any("dropped" in n for n in out.notes)


def test_the_queue_is_still_refilled_after_a_mail_failure(ledger_path, tmp_path):
    """A mail problem must not stop submissions -- that is the opposite of what
    it calls for."""
    led = write(ledger_path, [
        chunk("ELBA", "2025-09-09T00:00:00", state="submitted",
              email="you+a1@gmail.com", submitted_at=now()),
        chunk("ELBA", "2025-10-01T00:00:00"),
    ])
    box = DyingMailbox(NoData(b"1", "you+a1@gmail.com"))
    out = cycle(led, Pool("you@gmail.com", 1), Config(out_dir=str(tmp_path)),
                box, FakePortal(), log=quiet)
    assert out.submitted == ["ELBA:2025-10-01T00:00:00"]


def test_the_rest_of_the_pass_is_abandoned_rather_than_retried(ledger_path, tmp_path):
    """Every further consume on a dead socket raises the same way; grinding
    through them fills the log with one traceback per unread message."""
    led = write(ledger_path, [chunk("ELBA", "2025-09-09T00:00:00",
                                    state="submitted", email="you+a1@gmail.com",
                                    submitted_at=now())])
    seen = []

    class Counting(DyingMailbox):
        def consume(self, conn, uid, ok=True):
            seen.append(uid)
            super().consume(conn, uid, ok)

    box = Counting(NoData(b"1", "you+a1@gmail.com"),
                   NoData(b"2", "you+a1@gmail.com"),
                   NoData(b"3", "you+a1@gmail.com"))
    cycle(led, Pool("you@gmail.com", 1), Config(out_dir=str(tmp_path)),
          box, FakePortal(), log=quiet)
    assert seen == [b"1"], f"kept going on a dead socket: {seen}"


# --- 3. the code that could not exist --------------------------------------

def test_a_network_qualified_code_is_reduced_to_the_bare_one():
    """The portal lists 390 TU stations and not one code contains a dot. The
    network is already in the submission payload."""
    assert normalise("TU.KAND") == "KAND"
    assert normalise("KAND") == "KAND"
    assert normalise(" kand ") == "KAND"


class FakeCodes:
    def __init__(self, *codes):
        self.codes = list(codes)

    def station_codes(self):
        return self.codes


def test_an_unlistable_station_is_refused_at_plan_time():
    with pytest.raises(SystemExit, match="does not list"):
        verify("NOPE", FakeCodes("KAND", "ELBA", "NOBL"))


def test_the_refusal_names_near_matches():
    """"not in the station list" is not actionable. "did you mean NOBL" is."""
    with pytest.raises(SystemExit) as e:
        verify("NOXX", FakeCodes("KAND", "NOBL", "NOMN", "ELBA"))
    assert "NOBL" in str(e.value) and "NOMN" in str(e.value)


def test_a_listed_station_passes():
    verify("KAND", FakeCodes("KAND", "ELBA"))


def test_an_unreachable_portal_does_not_block_planning(capsys):
    """Planning is local bookkeeping, and the campaign may simply be offline."""
    class Broken:
        def station_codes(self):
            raise OSError("network unreachable")

    verify("KAND", Broken())
    assert "unverified" in capsys.readouterr().out
