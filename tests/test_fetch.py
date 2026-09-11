"""What arrived, and the four ways it can look like data and not be.

Every branch here cost real bytes before it existed: an expired link written
into a .zip, a 22-byte archive retired as permanent, a no-data notice banked as
a successful fetch for three windows, and an archive filed under another
window's name that a later download overwrote.
"""
import zipfile

import pytest

from conftest import chunk, write
from tdvms import fetch


def zip_with(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return path


MEMBER = "TU_ELBA_09092025_000000_30092025_000000_HH.mseed"
NOTICE = ("Secilen istasyon icerisinde gecerli zaman araliginda "
          "veri bulunamamistir.")


def test_the_window_comes_from_the_archive_not_the_ledger():
    """With eight slots in flight the links arrive interleaved, so "the oldest
    submitted chunk" is simply the wrong answer and files one window's data
    under another window's name."""
    station, start, end = fetch.window_from_member(MEMBER)
    assert station == "ELBA"
    assert start.strftime("%Y-%m-%d") == "2025-09-09"
    assert end.strftime("%Y-%m-%d") == "2025-09-30"


def test_a_name_that_is_not_a_tdvms_member_yields_nothing_rather_than_a_guess():
    assert fetch.window_from_member("something_else.mseed") == (None, None, None)


def test_an_empty_archive_is_transient_not_permanent(tmp_path):
    """GCAM 2024-09-04..09-25 came back as 22 bytes while both neighbouring
    windows hold ~700 MB. The station was recording; the portal failed to build
    the archive. Retiring it threw away real data."""
    p = zip_with(tmp_path / "e.zip", {})
    members, size, problem = fetch.inspect(p)
    assert members is None
    assert isinstance(problem, fetch.EmptyArchive)


def test_a_corrupt_file_is_not_treated_as_an_archive(tmp_path):
    p = tmp_path / "bad.zip"
    p.write_bytes(b"this is not a zip")
    members, size, problem = fetch.inspect(p)
    assert isinstance(problem, fetch.Expired)


def test_a_no_data_notice_is_a_valid_archive_and_must_not_pass_as_data(tmp_path):
    """It is a well-formed zip carrying one 82-byte notice. It passes every
    emptiness check and was banked as a successful fetch for three windows
    before anyone looked at the byte counts."""
    p = zip_with(tmp_path / "n.zip", {"TU_ELBA_09092025_000000_30092025_000000.txt": NOTICE})
    members, size, problem = fetch.inspect(p)
    assert problem is None, "structurally it is a perfectly good archive"
    assert not any(n.lower().endswith(".mseed") for n in members), (
        "requiring waveform members, not merely members, is the whole check")


def test_a_real_archive_is_filed_under_its_own_station(tmp_path):
    """Taking the station from the first ledger row wrote one station's archives
    into another station's directory."""
    p = zip_with(tmp_path / "ok.zip", {MEMBER: b"x" * 100})
    members, size, _ = fetch.inspect(p)
    row = chunk("ELBA", "2025-09-09T00:00:00")
    result = fetch.file_archive(p, row, tmp_path / "raw", members, size)
    assert isinstance(result, fetch.Fetched)
    assert result.path == tmp_path / "raw" / "ELBA" / "ELBA_2025-09-09.zip"
    assert result.path.exists()


def test_an_existing_archive_is_never_silently_overwritten(tmp_path):
    """A mislabelled earlier download left the wrong window under this name, and
    the corrected download overwrote 825 MB of good data."""
    raw = tmp_path / "raw" / "ELBA"
    raw.mkdir(parents=True)
    keeper = raw / "ELBA_2025-09-09.zip"
    keeper.write_bytes(b"the good 825 MB")

    p = zip_with(tmp_path / "ok.zip", {MEMBER: b"x" * 100})
    members, size, _ = fetch.inspect(p)
    result = fetch.file_archive(p, chunk("ELBA", "2025-09-09T00:00:00"),
                                tmp_path / "raw", members, size)
    assert not isinstance(result, fetch.Fetched)
    assert keeper.read_bytes() == b"the good 825 MB"
    assert len(list(raw.glob("*.dup*.zip"))) == 1, "both are kept for inspection"


def test_force_overwrites_when_the_operator_asks(tmp_path):
    raw = tmp_path / "raw" / "ELBA"
    raw.mkdir(parents=True)
    (raw / "ELBA_2025-09-09.zip").write_bytes(b"old")
    p = zip_with(tmp_path / "ok.zip", {MEMBER: b"x" * 100})
    members, size, _ = fetch.inspect(p)
    result = fetch.file_archive(p, chunk("ELBA", "2025-09-09T00:00:00"),
                                tmp_path / "raw", members, size, force=True)
    assert isinstance(result, fetch.Fetched)
