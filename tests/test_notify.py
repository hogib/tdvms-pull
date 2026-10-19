"""Notifications: once when something needs a person, once when it clears.

A loop that runs for days is mostly ignored, which is the point -- until the
one cycle that mattered goes by unseen. A notification that repeats every
minute is ignored just the same, so each condition speaks when it starts.
"""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import chunk, write
from tdvms import notify
from tdvms.slots import Pool
from tdvms.supervisor import Cycle


class Inbox:
    """A notifier that keeps what it was asked to send."""

    def __init__(self):
        self.sent = []

    def __bool__(self):
        return True

    def send(self, title, body, urgent=False):
        self.sent.append((title, urgent))
        return []


def ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


@pytest.fixture
def watch(ledger_path):
    led = write(ledger_path, [chunk(state="submitted", submitted_at=ago(0.1)),
                              chunk(start="2025-10-01T00:00:00", state="fetched",
                                    fetched_at=ago(0.2))])
    inbox = Inbox()
    return notify.Watch(inbox, "van", led, Pool("you@gmail.com", 2)), inbox, led


def test_a_mailbox_failing_for_three_cycles_speaks_once_then_once_when_it_mends(watch):
    w, inbox, _ = watch
    bad = Cycle()
    bad.mail_error = "IMAP4.error: [AUTHENTICATIONFAILED]"
    for _ in range(5):
        w.after(bad)
    assert inbox.sent == [("van: can't read the mailbox", True)]
    w.after(Cycle())
    assert inbox.sent[-1] == ("van: mailbox readable again", False)


def test_one_failed_read_is_not_worth_a_notification(watch):
    """IMAP drops are routine on a loop that runs for days."""
    w, inbox, _ = watch
    bad = Cycle()
    bad.mail_error = "abort: socket closed"
    w.after(bad)
    w.after(Cycle())
    assert inbox.sent == []


def test_a_full_disk_is_raised_and_cleared(watch):
    w, inbox, _ = watch
    full = Cycle()
    full.paused = "1.0 GB free"
    w.after(full)
    w.after(full)
    w.after(Cycle())
    assert [t for t, _ in inbox.sent] == ["van: disk nearly full", "van: disk space recovered"]


def test_completion_is_announced_once(watch):
    w, inbox, led = watch
    led.update("ELBA:2025-09-09T00:00:00", state="nodata")
    w.after(Cycle())
    w.after(Cycle())
    assert [t for t, _ in inbox.sent] == ["van: complete"]


def test_a_campaign_already_complete_when_the_loop_starts_is_not_announced(ledger_path):
    led = write(ledger_path, [chunk(state="fetched")])
    inbox = Inbox()
    notify.Watch(inbox, "van", led, Pool("you@gmail.com", 1)).after(Cycle())
    assert inbox.sent == []


def test_hours_with_requests_out_and_nothing_back_is_a_stall(ledger_path):
    led = write(ledger_path, [chunk(state="submitted", submitted_at=ago(4)),
                              chunk(start="2025-10-01T00:00:00", state="fetched",
                                    fetched_at=ago(5))])
    inbox = Inbox()
    notify.Watch(inbox, "van", led, Pool("you@gmail.com", 1)).after(Cycle())
    assert inbox.sent == [("van: nothing back for 3 h", True)]


def test_every_slot_cooling_down_is_raised(watch):
    w, inbox, _ = watch
    for slot in w.pool.slots.values():
        slot.mark_busy(600)
    w.after(Cycle())
    assert inbox.sent == [("van: every slot is cooling down", True)]


def test_retired_stations_are_named(watch):
    w, inbox, _ = watch
    c = Cycle()
    c.retired = ["BLKS:2024-01-01T00:00:00", "BLKS:2024-01-22T00:00:00"]
    w.after(c)
    assert inbox.sent == [("van: stopped requesting BLKS", False)]


def test_the_command_line_wins_then_the_environment_then_the_saved_file(tmp_path):
    saved = tmp_path / "notify.env"
    notify.save("https://ntfy.sh/saved", saved)
    assert notify.targets(None, {}, saved) == ["https://ntfy.sh/saved"]
    assert notify.targets(None, {"TDVMS_NOTIFY": "desktop, https://x/y"}, saved) == [
        "desktop", "https://x/y"]
    assert notify.targets(["desktop"], {"TDVMS_NOTIFY": "https://x/y"}, saved) == ["desktop"]


def test_a_url_target_gets_the_message_as_a_post(tmp_path):
    posted = []

    class Session:
        def post(self, url, data=None, headers=None, timeout=None):
            posted.append((url, data, headers["Title"]))

            class R:
                def raise_for_status(self):
                    pass
            return R()
    failed = notify.Notifier(["https://ntfy.sh/t"], session=Session()).send("van: complete",
                                                                           "4 fetched")
    assert failed == [] and posted == [("https://ntfy.sh/t", b"4 fetched", b"van: complete")]


def test_a_notification_that_fails_is_logged_and_never_raised(monkeypatch):
    monkeypatch.setattr(notify.shutil, "which", lambda name: None)
    logged = []
    failed = notify.Notifier(["desktop"], log=logged.append).send("t", "b")
    assert failed and "notify-send" in failed[0][1] and logged
