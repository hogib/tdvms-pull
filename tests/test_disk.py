"""The disk guard: nothing downloaded or submitted without room to keep it.

A download that runs out of space dies half-written, and the mail carrying its
link must then stay unread or the link is gone with it. Raw folders on this
campaign were moved between disks by hand to make room; the loop now checks
for itself.
"""
import errno

import pytest

from conftest import FakeMailbox, FakePortal, chunk, write
from tdvms import disk, fetch
from tdvms.ledger import now
from tdvms.mailbox import Link
from tdvms.slots import Pool
from tdvms.supervisor import Config, cycle

URL = "https://tdvms.afad.gov.tr/files/TDVMS_1_x.zip"


def quiet(*a, **k):
    pass


def test_the_reserve_is_twice_the_largest_archive_with_a_floor():
    rows = [chunk(state="fetched", bytes=1_200_000_000), chunk(state="fetched", bytes=5)]
    assert disk.reserve_bytes(rows) == 2_400_000_000
    assert disk.reserve_bytes([chunk(state="fetched", bytes=10)]) == disk.FLOOR_GB * 1e9
    assert disk.reserve_bytes(rows, min_free_gb=50) == 50e9


def test_zero_turns_the_guard_off(monkeypatch, tmp_path):
    monkeypatch.setattr(disk, "free_bytes", lambda p: 0)
    assert disk.check(tmp_path, [], min_free_gb=0) is None
    assert "below" in disk.check(tmp_path, [])


def test_a_folder_not_yet_created_is_measured_on_its_disk(tmp_path):
    assert disk.free_bytes(tmp_path / "not" / "yet" / "there") > 0


def _low(monkeypatch):
    monkeypatch.setattr(disk, "free_bytes", lambda p: 1e9)   # below the 2 GB floor


def test_short_of_room_nothing_is_submitted_and_links_stay_unread(ledger_path, tmp_path,
                                                                  monkeypatch):
    _low(monkeypatch)
    led = write(ledger_path, [
        chunk(state="submitted", email="you+a1@gmail.com", submitted_at=now()),
        chunk(start="2025-10-01T00:00:00")])
    box, portal = FakeMailbox(Link(b"1", "you+a1@gmail.com", [URL])), FakePortal()
    out = cycle(led, Pool("you@gmail.com", 2), Config(out_dir=str(tmp_path)), box,
                portal, log=quiet)
    assert out.paused and "below" in out.paused
    assert portal.calls == [] and box.consumed == []
    assert led.rows()[0]["state"] == "submitted"


def test_a_disk_that_fills_mid_download_leaves_the_mail_unread(ledger_path, tmp_path,
                                                                monkeypatch):
    led = write(ledger_path, [chunk(state="submitted", email="you+a1@gmail.com",
                                    submitted_at=now())])

    def full(url, out_dir):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(fetch, "download", full)
    box = FakeMailbox(Link(b"1", "you+a1@gmail.com", [URL]))
    out = cycle(led, Pool("you@gmail.com", 1), Config(out_dir=str(tmp_path)), box,
                FakePortal(), log=quiet)
    assert out.paused and box.consumed == []
    assert led.rows()[0]["state"] == "submitted"


def test_a_transfer_that_dies_takes_its_partial_file_with_it(tmp_path, monkeypatch):
    class Dying:
        headers = {"Content-Type": "application/zip"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, n):
            yield b"x" * 1000
            raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(fetch.requests, "get", lambda url, **kw: Dying())
    with pytest.raises(OSError):
        fetch.download(URL, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_with_room_the_cycle_is_unchanged(ledger_path, tmp_path):
    led = write(ledger_path, [chunk()])
    portal = FakePortal()
    out = cycle(led, Pool("you@gmail.com", 1), Config(out_dir=str(tmp_path)), None,
                portal, log=quiet)
    assert out.paused is None and len(portal.calls) == 1
