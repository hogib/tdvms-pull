"""The address pool: which queue slots exist, and which are genuinely free.

Not a runnable script -- imported only.

TDVMS keys its one-request-at-a-time limit on the literal address string, so
`you+a1@gmail.com` and `you+a2@gmail.com` are separate slots while the mail all
lands in one inbox. The pool is therefore derived from one address and a count,
not typed out. Hand-maintaining it produced a live campaign whose slots were
`+a1..a7`, `+e1..e4`, `+m1..m8` and a lone `+n1` -- four naming schemes, no
record of which belonged to which station, and no way to tell an idle slot from
one that was never created.

**A slot has two states, and they disagree more often than is comfortable.**

*Local* is what the ledger says the address holds. *Remote* is what the portal
last said to it. Marking a chunk `failed` clears the local state and frees
nothing at the portal: BLKS was retired locally after 26 straight no-data
answers, and the next submissions to `+m2` and `+m5` came straight back as
`[111] BUSY` because AFAD was still holding the abandoned requests. A slot is
submittable only when BOTH agree, which is what `busy_until` below encodes.
"""
from datetime import datetime, timedelta, timezone

from tdvms.ledger import IN_FLIGHT


def pool(address, slots):
    """`("you@gmail.com", 3)` -> `["you+a1@gmail.com", ...]`.

    Raises:
        ValueError: If the address is not plus-addressable, or `slots` is not
            at least 1. A bare local part with no `@` would produce addresses
            TDVMS accepts and no mail server delivers, which looks exactly like
            a portal that has stopped answering.
    """
    if slots < 1:
        raise ValueError(f"a pool needs at least one slot, got {slots}")
    if "@" not in address:
        raise ValueError(f"{address!r} is not an email address")
    local, domain = address.rsplit("@", 1)
    if "+" in local:
        raise ValueError(
            f"{address!r} is already a plus-address; give the base address "
            f"(e.g. {local.split('+')[0]}@{domain}) and let the pool extend it")
    return [f"{local}+a{i}@{domain}" for i in range(1, slots + 1)]


class Slot:
    """One address, its ledger chunk, and the portal's last word to it."""

    def __init__(self, email):
        self.email = email
        self.holding = None          # the ledger row occupying it, or None
        self.busy_until = None       # datetime; set when the portal says 111
        self.last_result = None      # the portal's last answer, for reporting

    @property
    def free(self):
        """Both halves agree that this slot can take a request."""
        return self.holding is None and not self.remote_busy

    @property
    def remote_busy(self):
        if self.busy_until is None:
            return False
        return datetime.now(timezone.utc) < self.busy_until

    def mark_busy(self, cooldown_seconds):
        """The portal answered 111: it still holds a request for this address.

        Backing off rather than retrying immediately is the whole point. A loop
        that re-submits into a 111 burns a cycle every tick and teaches the
        operator to ignore the log.
        """
        self.busy_until = (datetime.now(timezone.utc)
                           + timedelta(seconds=cooldown_seconds))
        self.last_result = "busy"

    def __repr__(self):
        where = self.holding["state"] if self.holding else "free"
        return f"<Slot {self.email} {where}{' busy' if self.remote_busy else ''}>"


class Pool:
    """The slots, refreshed from the ledger each cycle.

    Remote state lives here and only here, in memory: it is a fact about a
    conversation with the portal, not about the campaign, and persisting it
    would make a restarted `run` honour a cooldown for a request the portal has
    long since finished.
    """

    def __init__(self, address, slots):
        self.slots = {e: Slot(e) for e in pool(address, slots)}

    def refresh(self, ledger):
        """Re-reads which chunk each address holds. Remote state is preserved."""
        held = {}
        for r in ledger.rows():
            if r["state"] in IN_FLIGHT and r.get("email"):
                held[r["email"].strip().lower()] = r
        for email, slot in self.slots.items():
            slot.holding = held.get(email.lower())
        return self

    def get(self, email):
        """The slot for an address, matched case-insensitively.

        Mail servers do not agree on case and TDVMS echoes back whatever it was
        given, so a link delivered to `You+A3@...` must still name slot `+a3`.
        """
        if not email:
            return None
        e = email.strip().lower()
        return next((s for s in self.slots.values() if s.email.lower() == e), None)

    def free(self):
        return [s for s in self.slots.values() if s.free]

    def addresses(self):
        return list(self.slots)

    def __len__(self):
        return len(self.slots)
