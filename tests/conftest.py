"""Fakes for the two things this tool cannot own: the portal and the inbox.

Both are injected into `supervisor.cycle`, which is the reason it takes them as
arguments at all. The loop is then a pure function of (ledger, inbox, portal),
and the failures worth testing -- a slot freed but never refilled, a reset that
lands on two stations -- are assertions about a ledger rather than about a
network.
"""
import email.message
import json

import pytest

from tdvms.client import Accepted, AcceptedUnconfirmed, Busy, Rejected
from tdvms.ledger import Ledger
from tdvms.slots import Pool


@pytest.fixture
def ledger_path(tmp_path):
    return tmp_path / "l.jsonl"


def write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return Ledger(path)


def chunk(station="ELBA", start="2025-09-09T00:00:00", state="pending", **kw):
    row = {"station": station, "start": start,
           "end": start[:8] + f"{int(start[8:10]) + 21:02d}" + start[10:]
           if False else "2025-09-30T00:00:00",
           "state": state, "email": None, "url": None, "bytes": None,
           "note": None, "attempts": 0, "claimed_at": None,
           "submitted_at": None, "fetched_at": None}
    row.update(kw)
    return row


@pytest.fixture
def pool():
    return Pool("you@gmail.com", 3)


class FakePortal:
    """Answers submissions from a script, and records what it was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def submit(self, station, start, end, email):
        self.calls.append((station, start.strftime("%Y-%m-%d"), email))
        return self.answers.pop(0) if self.answers else Accepted(0, "ok")


class FakeConn:
    def __init__(self):
        self.flagged = []

    def logout(self):
        pass


class FakeMailbox:
    """Hands back a fixed list of events and records what was consumed."""

    def __init__(self, *events, boom=None):
        self.events = list(events)
        self.boom = boom
        self.consumed = []
        self.conn = FakeConn()

    def read(self, addresses, claim_unknown=False):
        if self.boom:
            raise self.boom
        return list(self.events), self.conn

    def consume(self, conn, uid, ok=True):
        self.consumed.append((uid, ok))


def message(to, body, sender="noreply@tdvms.afad.gov.tr", subject="TDVMS"):
    m = email.message.EmailMessage()
    m["To"], m["From"], m["Subject"] = to, sender, subject
    m.set_content(body)
    return m


@pytest.fixture
def portal():
    return FakePortal()


__all__ = ["write", "chunk", "FakePortal", "FakeMailbox", "message",
           "Accepted", "AcceptedUnconfirmed", "Busy", "Rejected"]
