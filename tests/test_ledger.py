"""Chunk identity, and the lost write.

Two of this campaign's costliest mistakes are structural, not logical: a chunk
that could only be named by date, and a whole-file write-back that discarded
whatever another process had recorded meanwhile. Both are properties of the
ledger, so both are pinned here.
"""
import json

import pytest

from conftest import chunk, write
from tdvms.ledger import Ledger, age_seconds, chunk_id


def test_a_chunk_is_named_by_station_and_start(ledger_path):
    led = write(ledger_path, [chunk("ELBA"), chunk("MANT")])
    assert led.by_id("ELBA:2025-09-09T00:00:00")["station"] == "ELBA"
    assert led.by_id("MANT:2025-09-09T00:00:00")["station"] == "MANT"


def test_updating_one_station_leaves_the_other_alone(ledger_path):
    """The `reset --start 2024-09-17` incident: one date, three rows, two
    stations, two of them already answered. There is no date-keyed write here,
    so the only way to hit two stations is to ask twice."""
    led = write(ledger_path, [chunk("ELBA"), chunk("MANT"), chunk("SEMS")])
    led.update("ELBA:2025-09-09T00:00:00", state="fetched")
    states = {r["station"]: r["state"] for r in led.rows()}
    assert states == {"ELBA": "fetched", "MANT": "pending", "SEMS": "pending"}


def test_a_write_does_not_clobber_a_concurrent_one(ledger_path):
    """`fetch` holds its view for the minutes a download takes. Writing that
    view back erased a submission recorded meanwhile, and the freed window was
    handed to a second address -- a duplicate the portal then refused."""
    led = write(ledger_path, [chunk("ELBA"), chunk("MANT")])
    stale = led.rows()                                  # a view taken early
    led.update("MANT:2025-09-09T00:00:00", state="submitted", email="you+a2@x")
    assert stale[1]["state"] == "pending", "the stale view is genuinely stale"
    led.update("ELBA:2025-09-09T00:00:00", state="fetched")
    assert led.by_id("MANT:2025-09-09T00:00:00")["state"] == "submitted"


def test_claim_takes_the_oldest_window_not_the_first_line(ledger_path):
    """The old claim was `next(r for r in rows if pending)` -- first in the
    FILE. A station appended later jumped ahead of windows that had been
    waiting since the campaign began."""
    led = write(ledger_path, [chunk("SEMS", "2025-12-01T00:00:00"),
                              chunk("ELBA", "2024-05-01T00:00:00")])
    row, blocker = led.claim("you+a1@x")
    assert blocker is None
    assert row["station"] == "ELBA", "the 2024 window is older than the 2025 one"


def test_one_address_cannot_hold_two_chunks(ledger_path):
    led = write(ledger_path, [chunk("ELBA"), chunk("MANT")])
    first, _ = led.claim("you+a1@x")
    second, blocker = led.claim("you+a1@x")
    assert second is None
    assert blocker["station"] == first["station"]


def test_a_claim_is_stamped_so_it_can_be_timed_out(ledger_path):
    """Without the stamp a row that died mid-POST sat forever. SLV held a slot
    for a day and a half, indistinguishable from mail that was merely slow."""
    led = write(ledger_path, [chunk("ELBA")])
    led.claim("you+a1@x")
    row = led.by_id("ELBA:2025-09-09T00:00:00")
    assert row["state"] == "claimed"
    assert row["claimed_at"] is not None
    assert age_seconds(row["claimed_at"]) < 5


def test_release_clears_the_stamps_it_invalidates(ledger_path):
    """A requeued row keeping its old `fetched_at` made the status line subtract
    two unrelated events and report `range -3064-76 min`."""
    led = write(ledger_path, [chunk("ELBA", state="fetched",
                                    submitted_at="2025-09-09T00:00:00+00:00",
                                    fetched_at="2025-09-09T02:00:00+00:00",
                                    url="http://x/y.zip", bytes=1)])
    led.release("ELBA:2025-09-09T00:00:00")
    row = led.by_id("ELBA:2025-09-09T00:00:00")
    assert row["state"] == "pending"
    assert row["submitted_at"] is None and row["fetched_at"] is None
    assert row["email"] is None and row["url"] is None and row["bytes"] is None


def test_an_interrupted_write_cannot_truncate_the_ledger(ledger_path):
    """The write is an atomic replace: a killed process leaves the old file
    whole, not a half-line that makes all 137 chunks unreadable."""
    led = write(ledger_path, [chunk("ELBA"), chunk("MANT")])
    led.update("ELBA:2025-09-09T00:00:00", state="fetched")
    lines = ledger_path.read_text().splitlines()
    assert len(lines) == 2
    assert all(json.loads(l) for l in lines)


def test_an_unstamped_row_has_unknown_age_not_zero_age():
    """Reporting a missing stamp as "0 seconds old" makes the oldest stall in
    the file look like the freshest thing in it."""
    assert age_seconds(None) is None
    assert age_seconds("not a date") is None
