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
import json
import os
import pathlib
from datetime import datetime, timedelta, timezone

from tdvms import compat
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


class PoolHeld(Exception):
    """Another live process is already working this address pool."""


def lock_path(address, root=None):
    """Where the pool lock for a base address lives. Per user, not per ledger."""
    base = pathlib.Path(root) / "tdvms" if root else compat.state_dir()
    return base / f"{address.strip().lower()}.lock"


def relabel(fh, ledger_path, **info):
    """Rewrites what a held pool lock says about its holder.

    JSON: the pid and ledger, plus whatever the run adds -- its command,
    address, slots, download folder, log -- which is how `tdvms-tui` finds a
    running download on any OS, `/proc` or not. `queue` calls this again each
    time it moves to the next campaign.
    """
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps({"pid": os.getpid(),
                         "ledger": str(pathlib.Path(ledger_path).resolve()), **info}))
    fh.write("\n")
    fh.flush()


def describe(text):
    """`"pid N, ledger PATH"` from a lock file's text, old format or new."""
    try:
        info = json.loads(text)
        return f"pid {info['pid']}, ledger {info['ledger']}"
    except (ValueError, KeyError, TypeError):
        return text.strip() or "another process"


def holder(address, root=None):
    """`"pid N, ledger PATH"` if a live process holds this pool, else None.

    Probes the lock rather than trusting the file's text: the text outlives its
    writer, the lock does not.
    """
    path = lock_path(address, root)
    if not path.exists():
        return None
    with open(path) as fh:
        if compat.held(fh):
            return describe(fh.read())
    return None


def holders(root=None):
    """What every live run on this machine says about itself, as dicts."""
    base = pathlib.Path(root) / "tdvms" if root else compat.state_dir()
    out = []
    for path in sorted(base.glob("*.lock")) if base.is_dir() else []:
        try:
            with open(path) as fh:
                if not compat.held(fh):
                    continue
                info = json.loads(fh.read())
        except (OSError, ValueError):
            continue
        if isinstance(info, dict) and "pid" in info:
            out.append(info)
    return out


def hold(address, ledger_path, root=None, **info):
    """Takes the address pool for this process, or says who already has it.

    Two runs on one base address submit from the same `+aN` slots and read the
    same inbox, and a link to a slot is acted on by whichever reads it first.
    That ran for hours: a finished balikesir run, which ignored the SIGINT meant
    to stop it, downloaded eleven of silivri's archives, filed them UNMATCHED in
    its own directory and marked the mail read -- leaving silivri's slots to
    wait out the submit timeout and ask for the same windows again.

    The lock is keyed by the address, not the ledger, because the collision is
    on the address. It lives in a per-user directory rather than next to the
    ledger so two ledgers in two directories still meet on it, and the OS drops
    it when the holder dies, so it can never go stale.

    Returns:
        The open lock file. Keep a reference for as long as the pool is in use.

    Raises:
        PoolHeld: Naming the holder's pid and ledger.
    """
    path = lock_path(address, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        compat.lock(fh, block=False)
    except BlockingIOError:
        fh.seek(0)
        who = describe(fh.read())
        fh.close()
        raise PoolHeld(who) from None
    relabel(fh, ledger_path, address=address, **info)
    return fh


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
