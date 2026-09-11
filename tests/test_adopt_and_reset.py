"""Migration off the old ledger, and the reset that could not be aimed.

Both are about identity. The old ledger had no chunk id, so `reset` matched by
date across every station in the file; and it had no retry counter and stamps
that a reset could invert.
"""
import json

import pytest

from conftest import chunk, write
from tdvms.adopt import convert
from tdvms.ledger import Ledger
from tdvms.reset import resolve


def old(station, start, **kw):
    row = {"station": station, "start": start, "end": "2025-09-30T00:00:00",
           "state": "fetched", "email": "you+a1@gmail.com", "url": "http://x/y.zip",
           "bytes": 1000, "note": None}
    row.update(kw)
    return row


# --- adopt -----------------------------------------------------------------

def test_every_adopted_row_gets_a_retry_budget():
    rows, _ = convert([old("ELBA", "2025-09-09T00:00:00")])
    assert rows[0]["attempts"] == 0


def test_stamps_that_invert_are_dropped_rather_than_carried():
    """A reset kept `fetched_at` and cleared `submitted_at`. Subtracting the
    survivor from a later attempt printed `range -3064-76 min` -- arithmetic
    across two unrelated events, not a fast request."""
    rows, repairs = convert([old("ELBA", "2025-09-09T00:00:00",
                                 submitted_at="2025-09-10T00:00:00+00:00",
                                 fetched_at="2025-09-09T00:00:00+00:00")])
    assert rows[0]["submitted_at"] is None and rows[0]["fetched_at"] is None
    assert repairs and "fetched before submitted" in repairs[0]


def test_a_fetch_with_no_submission_is_the_same_inversion():
    rows, repairs = convert([old("ELBA", "2025-09-09T00:00:00",
                                 fetched_at="2025-09-09T00:00:00+00:00")])
    assert rows[0]["fetched_at"] is None
    assert repairs


def test_an_honest_turnaround_survives_import():
    rows, repairs = convert([old("ELBA", "2025-09-09T00:00:00",
                                 submitted_at="2025-09-09T00:00:00+00:00",
                                 fetched_at="2025-09-09T02:00:00+00:00")])
    assert rows[0]["submitted_at"] and rows[0]["fetched_at"]
    assert repairs == []


def test_a_claimed_row_is_stamped_at_import_not_invented():
    """It has no record of when it was claimed. Dating it to the import is
    honest, and lets the reaper time it out one interval later rather than
    immediately on no evidence."""
    rows, repairs = convert([old("SLV", "2025-09-09T00:00:00", state="claimed")],
                            stamp="2026-09-11T09:00:00+00:00")
    assert rows[0]["claimed_at"] == "2026-09-11T09:00:00+00:00"
    assert repairs and "claimed with no stamp" in repairs[0]


def test_adoption_preserves_every_chunk_and_its_state():
    source = [old("ELBA", "2025-09-09T00:00:00"),
              old("MANT", "2024-05-01T00:00:00", state="nodata"),
              old("BLKS", "2025-01-01T00:00:00", state="failed")]
    rows, _ = convert(source)
    assert len(rows) == 3
    assert [r["state"] for r in rows] == ["fetched", "nodata", "failed"]
    assert [r["station"] for r in rows] == ["ELBA", "MANT", "BLKS"]


# --- reset -----------------------------------------------------------------

def test_a_date_matching_two_stations_is_refused(ledger_path):
    """`reset --start 2024-09-17` was meant for one ELBA window. It hit three
    rows in two stations, two already answered `nodata`, and the slot spent
    re-requesting them was gone."""
    led = write(ledger_path, [chunk("ELBA", "2024-09-17T00:00:00"),
                              chunk("BLKS", "2024-09-17T00:00:00", state="nodata")])
    with pytest.raises(SystemExit, match="matches 2 chunks"):
        resolve(led, None, "2024-09-17")


def test_the_refusal_names_the_candidates(ledger_path):
    led = write(ledger_path, [chunk("ELBA", "2024-09-17T00:00:00"),
                              chunk("BLKS", "2024-09-17T00:00:00", state="nodata")])
    with pytest.raises(SystemExit) as e:
        resolve(led, None, "2024-09-17")
    assert "ELBA:2024-09-17" in str(e.value) and "BLKS:2024-09-17" in str(e.value)


def test_naming_the_station_resolves_it(ledger_path):
    led = write(ledger_path, [chunk("ELBA", "2024-09-17T00:00:00"),
                              chunk("BLKS", "2024-09-17T00:00:00", state="nodata")])
    assert resolve(led, "ELBA", "2024-09-17")["station"] == "ELBA"


def test_a_date_nothing_matches_says_so(ledger_path):
    led = write(ledger_path, [chunk("ELBA", "2024-09-17T00:00:00")])
    with pytest.raises(SystemExit, match="no chunk matches"):
        resolve(led, "ELBA", "2030-01-01")
