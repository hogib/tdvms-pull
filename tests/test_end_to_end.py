"""A link arrives, an archive lands, the slot refills -- in one process.

This is the property the whole project exists for. The tool it replaces needed
three programs and two subprocess calls to get from "mail arrived" to "next
request sent", and the seams between them are where every stall came from.
"""
import zipfile

import pytest

from conftest import Accepted, FakeMailbox, FakePortal, chunk, write
from tdvms.ledger import now
from tdvms.mailbox import Link
from tdvms.slots import Pool
from tdvms.supervisor import Config, cycle

MEMBER = "TU_ELBA_09092025_000000_30092025_000000_HH.mseed"
NOTICE = "Secilen istasyon icerisinde gecerli zaman araliginda veri bulunamamistir."


class FakeResponse:
    """A streamed download, without a network."""

    def __init__(self, payload, ctype="application/zip"):
        self.payload = payload
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, n):
        yield self.payload


def archive(members):
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return buf.getvalue()


@pytest.fixture
def serve(monkeypatch):
    """Points `fetch.download` at an in-memory archive."""
    def _serve(payload, ctype="application/zip"):
        from tdvms import fetch
        monkeypatch.setattr(fetch.requests, "get",
                            lambda url, **kw: FakeResponse(payload, ctype))
    return _serve


def submitted(station, start, email="you+a1@gmail.com"):
    return chunk(station, start, state="submitted", email=email, submitted_at=now())


def test_a_link_lands_an_archive_and_the_freed_slot_is_refilled(tmp_path, serve):
    serve(archive({MEMBER: b"x" * 5000}))
    led = write(tmp_path / "l.jsonl", [
        submitted("ELBA", "2025-09-09T00:00:00"),
        chunk("ELBA", "2025-10-01T00:00:00"),
    ])
    pool = Pool("you@gmail.com", 1)
    cfg = Config(out_dir=str(tmp_path / "raw"))
    box = FakeMailbox(Link(b"1", "you+a1@gmail.com",
                           ["https://tdvms.afad.gov.tr/files/a.zip"]))
    portal = FakePortal(Accepted(0, "ok"))

    out = cycle(led, pool, cfg, box, portal, log=lambda *_: None)

    assert out.fetched == ["ELBA:2025-09-09T00:00:00"]
    assert (tmp_path / "raw" / "ELBA" / "ELBA_2025-09-09.zip").exists()
    assert led.by_id("ELBA:2025-09-09T00:00:00")["state"] == "fetched"
    assert out.submitted == ["ELBA:2025-10-01T00:00:00"], (
        "the slot the archive freed was not refilled in the same cycle")
    assert box.consumed == [(b"1", True)]


def test_an_expired_link_requeues_its_window_and_frees_the_slot(tmp_path, serve):
    """An expired link returns AFAD's homepage as HTML with HTTP 200, not a
    404. Streaming it into a .zip and banking it is the failure this catches."""
    serve(b"<html>AFAD</html>", ctype="text/html")
    led = write(tmp_path / "l.jsonl", [submitted("ELBA", "2025-09-09T00:00:00")])
    pool = Pool("you@gmail.com", 1)
    box = FakeMailbox(Link(b"1", "you+a1@gmail.com",
                           ["https://tdvms.afad.gov.tr/files/a.zip"]))

    out = cycle(led, pool, Config(out_dir=str(tmp_path / "raw")), box,
                FakePortal(), log=lambda *_: None)

    assert out.requeued == ["ELBA:2025-09-09T00:00:00"]
    assert box.consumed == [(b"1", False)], "a failure is flagged, not silently read"
    # Requeued and then picked straight back up by step 4 of the SAME cycle.
    # That is the intended behaviour: the window is still wanted, the slot is
    # free, and waiting a full tick to notice would be the old tool's problem.
    row = led.by_id("ELBA:2025-09-09T00:00:00")
    assert row["attempts"] == 1, "the failed attempt was counted"
    assert out.submitted == ["ELBA:2025-09-09T00:00:00"]


def test_an_empty_archive_requeues_rather_than_retiring_the_window(tmp_path, serve):
    """22 bytes is a bare end-of-central-directory. GCAM's neighbouring windows
    hold ~700 MB each, so the station was recording and the portal simply failed
    to build the archive."""
    serve(archive({}))
    led = write(tmp_path / "l.jsonl", [submitted("ELBA", "2025-09-09T00:00:00")])
    out = cycle(led, Pool("you@gmail.com", 1), Config(out_dir=str(tmp_path / "raw")),
                FakeMailbox(Link(b"1", "you+a1@gmail.com",
                                 ["https://tdvms.afad.gov.tr/files/a.zip"])),
                FakePortal(), log=lambda *_: None)
    assert out.requeued == ["ELBA:2025-09-09T00:00:00"]
    assert led.by_id("ELBA:2025-09-09T00:00:00")["attempts"] == 1
    assert out.submitted == ["ELBA:2025-09-09T00:00:00"], (
        "the window still holds data, so it is re-requested rather than retired")


def test_a_notice_archive_is_recorded_as_nodata_not_as_data(tmp_path, serve):
    """It is a valid zip holding one 82-byte notice, and it was banked as a
    successful fetch for three windows before anyone read the byte counts."""
    serve(archive({"TU_ELBA_09092025_000000_30092025_000000.txt": NOTICE}))
    led = write(tmp_path / "l.jsonl", [submitted("ELBA", "2025-09-09T00:00:00")])
    out = cycle(led, Pool("you@gmail.com", 1), Config(out_dir=str(tmp_path / "raw")),
                FakeMailbox(Link(b"1", "you+a1@gmail.com",
                                 ["https://tdvms.afad.gov.tr/files/a.zip"])),
                FakePortal(), log=lambda *_: None)
    assert out.nodata == ["ELBA:2025-09-09T00:00:00"]
    assert out.fetched == []
    assert not (tmp_path / "raw" / "ELBA").exists()


def test_an_archive_for_a_window_we_do_not_hold_is_kept_not_guessed(tmp_path, serve):
    """The old fallback was "the oldest submitted chunk", which files one
    window's data under another window's name; a later corrected download then
    overwrote 825 MB of good data."""
    serve(archive({"TU_XXXX_01012030_000000_22012030_000000_HH.mseed": b"x" * 100}))
    led = write(tmp_path / "l.jsonl", [submitted("ELBA", "2025-09-09T00:00:00")])
    out = cycle(led, Pool("you@gmail.com", 1), Config(out_dir=str(tmp_path / "raw")),
                FakeMailbox(Link(b"1", "you+a1@gmail.com",
                                 ["https://tdvms.afad.gov.tr/files/a.zip"])),
                FakePortal(), log=lambda *_: None)
    assert out.fetched == [] and out.nodata == []
    assert led.by_id("ELBA:2025-09-09T00:00:00")["state"] == "submitted", (
        "an unmatched archive must not disturb a chunk that is still in flight")
    strays = list((tmp_path / "raw").glob("UNMATCHED_*.zip"))
    assert len(strays) == 1


def test_a_link_already_banked_does_not_refill_a_slot_that_never_freed(tmp_path, serve):
    """An older mail resurfacing. Treating a duplicate as a fresh fetch made the
    old poller refill a slot that was never occupied, and the portal said BUSY."""
    url = "https://tdvms.afad.gov.tr/files/a.zip"
    led = write(tmp_path / "l.jsonl", [
        chunk("ELBA", "2025-09-09T00:00:00", state="fetched", url=url, bytes=5000),
        submitted("MANT", "2025-09-09T00:00:00"),
    ])
    out = cycle(led, Pool("you@gmail.com", 1), Config(out_dir=str(tmp_path / "raw")),
                FakeMailbox(Link(b"1", "you+a1@gmail.com", [url])),
                FakePortal(), log=lambda *_: None)
    assert out.fetched == [] and out.submitted == []
    assert led.by_id("MANT:2025-09-09T00:00:00")["state"] == "submitted"


def test_a_cycle_interrupted_before_submitting_leaves_recoverable_state(tmp_path, serve):
    """The durability property: the ledger is the state, so whatever a crash
    interrupts, the next cycle can see and act on."""
    serve(archive({MEMBER: b"x" * 5000}))
    led = write(tmp_path / "l.jsonl", [
        submitted("ELBA", "2025-09-09T00:00:00"),
        chunk("ELBA", "2025-10-01T00:00:00"),
    ])
    cfg = Config(out_dir=str(tmp_path / "raw"))
    # Cycle one: mail only, no portal -- as if the process died before step 4.
    cycle(led, Pool("you@gmail.com", 1), cfg,
          FakeMailbox(Link(b"1", "you+a1@gmail.com",
                           ["https://tdvms.afad.gov.tr/files/a.zip"])),
          None, log=lambda *_: None)
    assert led.by_id("ELBA:2025-09-09T00:00:00")["state"] == "fetched"
    # Cycle two, a fresh pool as a restarted process would build: the slot the
    # first cycle freed is picked up with no trace of the interruption.
    out = cycle(led, Pool("you@gmail.com", 1), cfg, None, FakePortal(),
                log=lambda *_: None)
    assert out.submitted == ["ELBA:2025-10-01T00:00:00"]


def test_a_notice_whose_member_name_does_not_parse_still_names_its_window(
        tmp_path, serve):
    """The address is the fallback: a slot holds exactly one chunk at a time.
    Without it, a no-data answer that needs no download at all is the one answer
    that cannot be recorded, and the window waits out the full submit timeout
    for a link that is never coming."""
    serve(archive({"bildirim.txt": NOTICE}))
    led = write(tmp_path / "l.jsonl", [submitted("ELBA", "2025-09-09T00:00:00")])
    out = cycle(led, Pool("you@gmail.com", 1), Config(out_dir=str(tmp_path / "raw")),
                FakeMailbox(Link(b"1", "you+a1@gmail.com",
                                 ["https://tdvms.afad.gov.tr/files/a.zip"])),
                FakePortal(), log=lambda *_: None)
    assert out.nodata == ["ELBA:2025-09-09T00:00:00"]
