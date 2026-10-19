"""Coverage: what the archives really hold, read from miniSEED record headers.

A `fetched` window only says an archive arrived. Checked against ObsPy on real
TDVMS archives -- including one with ~500 gaps per channel -- the spans agree
exactly; these tests pin the format details and the gap accounting.
"""
import argparse
import struct
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from conftest import chunk, write
from tdvms import coverage

T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)


def record(start, nsamp=100, rate=100, cha="HHZ", sta="ELBA", net="TU", order=">"):
    """One 512-byte miniSEED 2 data record: fixed header plus blockette 1000."""
    head = struct.pack(order + "6sc1x5s2s3s2sHHBBB1xHHhhBBBBiHH",
                       b"000001", b"D", sta.ljust(5).encode(), b"  ", cha.encode(),
                       net.encode(), start.year, start.timetuple().tm_yday, start.hour,
                       start.minute, start.second, start.microsecond // 100, nsamp,
                       rate, 1, 0, 0, 0, 1, 0, 64, 48)
    b1000 = struct.pack(order + "HHBBBB", 1000, 0, 11, 1 if order == ">" else 0, 9, 0)
    return (head + b1000).ljust(512, b"\0")


def stream(*seconds, **kw):
    """Records starting at each of these offsets from T0, one second long each."""
    return b"".join(record(T0 + timedelta(seconds=s), **kw) for s in seconds)


class Bytes:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def read(self, n):
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out


def spans(data, block=1 << 22):
    return {cid: [[s - T0.timestamp(), e - T0.timestamp()] for s, e in sp]
            for cid, (rate, sp) in coverage.scan(Bytes(data), block).items()}


def test_back_to_back_records_are_one_span_and_a_missing_one_is_a_gap():
    assert spans(stream(0, 1, 2, 4, 5)) == {"TU.ELBA..HHZ": [[0, 3], [4, 6]]}


def test_channels_are_kept_apart():
    got = spans(stream(0, 1, cha="HHZ") + stream(0, cha="HHN"))
    assert got == {"TU.ELBA..HHZ": [[0, 2]], "TU.ELBA..HHN": [[0, 1]]}


def test_a_record_split_across_two_reads_is_still_read():
    assert spans(stream(0, 1, 2), block=700) == {"TU.ELBA..HHZ": [[0, 3]]}


def test_little_endian_records_are_read_too():
    assert spans(stream(0, 1, order="<")) == {"TU.ELBA..HHZ": [[0, 2]]}


def test_something_that_is_not_miniseed_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="not a miniSEED 2 record"):
        coverage.scan(Bytes(b"<html>expired link</html>".ljust(512, b" ")))


def test_the_sample_rate_rules_of_seed():
    assert coverage._rate(100, 1) == 100
    assert coverage._rate(-10, 1) == 0.1          # one sample every ten seconds
    assert coverage._rate(1, -10) == 0.1


def test_interval_arithmetic():
    assert coverage.subtract([[0, 10]], [[2, 3], [5, 6]]) == [[0, 2], [3, 5], [6, 10]]
    assert coverage.subtract([[0, 10]], []) == [[0, 10]]
    assert coverage.merge([[5, 6], [0, 2], [1, 3]]) == [[0, 3], [5, 6]]


# --- a campaign's report ---------------------------------------------------------

DAY = 86400


def _campaign(tmp_path, data):
    """ELBA: one fetched window holding `data`, one no-data, one not yet asked."""
    iso = lambda d: (T0 + timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%S")
    led = write(tmp_path / "elba_ledger.jsonl", [
        chunk("ELBA", iso(0), state="fetched", end=iso(1)),
        chunk("ELBA", iso(1), state="nodata", end=iso(2)),
        chunk("ELBA", iso(2), state="pending", end=iso(3))])
    raw = tmp_path / "elba_raw"
    (raw / "ELBA").mkdir(parents=True)
    with zipfile.ZipFile(raw / "ELBA" / "ELBA_2025-01-01.zip", "w") as zf:
        zf.writestr("TU_ELBA_01012025_000000_02012025_000000_HH.mseed", data)
    return led, raw


def test_every_gap_says_why_it_is_there(tmp_path):
    # Half a day of one-second records, the first half of the fetched window.
    led, raw = _campaign(tmp_path, stream(*range(0, DAY // 2)))
    [st] = coverage.report(led, raw)
    [ch] = st.channels
    assert ch.covered == DAY // 2 and ch.requested == 3 * DAY
    assert [(g.start - T0.timestamp(), g.end - T0.timestamp(), g.why) for g in ch.gaps] == [
        (DAY // 2, DAY, "data gap"), (DAY, 2 * DAY, "no data at source"),
        (2 * DAY, 3 * DAY, "not downloaded yet")]


def test_an_archive_is_read_once_and_then_answered_from_the_cache(tmp_path, monkeypatch):
    led, raw = _campaign(tmp_path, stream(0, 1, 2))
    first = coverage.report(led, raw)
    monkeypatch.setattr(coverage, "scan_archive",
                        lambda p: pytest.fail(f"read {p} again"))
    again = coverage.report(led, raw)
    assert again[0].channels[0].covered == first[0].channels[0].covered == 3


def test_the_timeline_marks_each_kind_of_hole(tmp_path):
    led, raw = _campaign(tmp_path, stream(*range(0, DAY // 2)))
    [st] = coverage.report(led, raw)
    kinds = [k for _, k in coverage.timeline(st, st.channels[0], 6)]
    assert kinds == ["full", "gap", "nodata", "nodata", "pending", "pending"]


def test_the_command_prints_a_line_per_channel(tmp_path, capsys):
    led, raw = _campaign(tmp_path, stream(*range(0, 600)))
    p = coverage.add_args(argparse.ArgumentParser())
    p.add_argument("--ledger")
    coverage.run(p.parse_args(["--ledger", str(led.path), "--gaps"]))
    out = capsys.readouterr().out
    assert "ELBA     HHZ" in out and "no data at source" in out


def test_the_cli_lists_coverage(capsys):
    from tdvms import cli
    cli.usage()
    assert "coverage" in capsys.readouterr().out
