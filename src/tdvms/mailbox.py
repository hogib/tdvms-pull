"""The inbox, read as events. It decides nothing and writes nothing.

Not a runnable script -- imported only.

A pass returns a list of `Link`, `NoData`, `Foreign` or `Unrelated`, and the
supervisor acts on them. That split is the point of this rewrite: the tool it
replaces marked a chunk by *shelling out* to its own CLI and then refilled the
slot with a second subprocess, so a successful `mark` followed by a failed
`next` left the ledger recording an outcome for a slot nobody refilled. Nothing
owned the pair. Here one process holds the lock across both.

Messages are fetched with `BODY.PEEK` and never flagged from here. A plain
fetch sets `\\Seen` as a side effect, which consumes a message whose link has
not been downloaded yet -- and the window it belonged to then waits forever for
mail that has already been burned.
"""
import email
import email.header
import email.utils
import imaplib
import os
import re

LINK_RE = re.compile(r'https?://tdvms\.afad\.gov\.tr/[^\s"\'<>\\]+\.zip')
# The portal's third way of saying "nothing here": a mail with no link at all.
# "...talep ettiginiz istasyon/istasyonlara ait veri bulunmamaktadir"
NODATA_RE = re.compile(r"veri\s+bulunmamaktad", re.IGNORECASE)


class Event:
    def __init__(self, uid, to, subject=""):
        self.uid, self.to, self.subject = uid, to, subject

    def __repr__(self):
        return f"{type(self).__name__}(uid={self.uid}, to={self.to!r})"


class Link(Event):
    """One or more download links, addressed to a slot this campaign owns."""

    def __init__(self, uid, to, urls, subject=""):
        super().__init__(uid, to, subject)
        self.urls = urls


class NoData(Event):
    """The portal answering "no waveform" with no link and no window.

    The address is enough to act on: a slot holds exactly one chunk at a time,
    so the address names the window unambiguously.
    """


class Foreign(Event):
    """Addressed to a slot this ledger does not own. Left unread, deliberately.

    One mailbox can serve several campaigns, and every poller runs the same
    search and sees every message. Whichever ticks first would otherwise
    consume a link belonging to another ledger, which then waits forever. That
    is not hypothetical: two of a probe's four requests were taken by another
    poller and logged as permanent failures while the owning ledger still
    listed them as submitted.
    """


class Unrelated(Event):
    """Not from the portal. Never touched -- this may be somebody's real inbox."""


class Unreadable(Event):
    """The fetch itself failed. Left unread so the next pass retries it."""


def recipient_of(msg):
    """Which slot this mail was delivered to.

    `To` is what the portal addressed. `Delivered-To` and `X-Original-To` are
    what the server actually routed, and survive forwarding setups that rewrite
    `To` -- without them a forwarded link names no slot and refills nothing.
    """
    for header in ("To", "Delivered-To", "X-Original-To"):
        for _, addr in email.utils.getaddresses(msg.get_all(header, [])):
            if "@" in addr:
                return addr.strip().lower()
    return None


def body_text(msg):
    """Every text part, concatenated.

    Walking all parts matters: these arrive as HTML often enough that reading
    only `text/plain` sees an empty message and reports no link at all.
    """
    out = []
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        try:
            raw = part.get_payload(decode=True)
        except Exception:
            continue
        if raw:
            out.append(raw.decode(part.get_content_charset() or "utf-8",
                                  errors="replace"))
    return "".join(out)


def classify(uid, msg, known_addresses, claim_unknown=False):
    """One message -> one event. Pure; used directly by the tests."""
    to = recipient_of(msg)
    subject = str(email.header.make_header(
        email.header.decode_header(msg.get("Subject", ""))))
    sender = (msg.get("From") or "").lower()
    if to and not claim_unknown and to not in known_addresses:
        return Foreign(uid, to, subject)
    urls = []
    for url in LINK_RE.findall(body_text(msg)):
        if url not in urls:
            urls.append(url)
    if urls:
        return Link(uid, to, urls, subject)
    text = body_text(msg)
    if NODATA_RE.search(text):
        return NoData(uid, to, subject)
    if "tdvms" in sender or "afad" in sender:
        # From the portal, but neither a link nor a notice. Consume it: a
        # message that matches the search and is never consumed is re-fetched
        # every tick forever. Three of them once ran 21 times in seven minutes.
        return Unrelated(uid, to, subject)
    return Unrelated(uid, to, subject)


class Mailbox:
    """An IMAP mailbox. Credentials come from the environment only."""

    def __init__(self, folder="INBOX", search="(UNSEEN)"):
        self.folder, self.search = folder, search

    @staticmethod
    def _credentials():
        env = {n: os.environ.get(n) for n in
               ("TDVMS_IMAP_HOST", "TDVMS_IMAP_USER", "TDVMS_IMAP_PASS")}
        # The campaign this replaces used AFAD_IMAP_*; accept both so a live
        # environment does not have to be re-exported mid-campaign.
        for new, old in (("TDVMS_IMAP_HOST", "AFAD_IMAP_HOST"),
                         ("TDVMS_IMAP_USER", "AFAD_IMAP_USER"),
                         ("TDVMS_IMAP_PASS", "AFAD_IMAP_PASS")):
            env[new] = env[new] or os.environ.get(old)
        missing = [n for n, v in env.items() if not v]
        if missing:
            raise SystemExit(
                f"missing environment: {', '.join(missing)}\n"
                f"  export TDVMS_IMAP_HOST=imap.gmail.com\n"
                f"  export TDVMS_IMAP_USER=you@gmail.com\n"
                f"  export TDVMS_IMAP_PASS='<app password, not the account password>'")
        return env["TDVMS_IMAP_HOST"], env["TDVMS_IMAP_USER"], env["TDVMS_IMAP_PASS"]

    def read(self, known_addresses, claim_unknown=False):
        """One pass. Returns `(events, connection)`; the caller consumes.

        The connection is handed back so the supervisor can flag exactly the
        messages it finished with, in the same pass, rather than flagging
        optimistically and losing a link whose download then failed.
        """
        host, user, password = self._credentials()
        conn = imaplib.IMAP4_SSL(host)
        conn.login(user, password)
        conn.select(self.folder)
        typ, data = conn.uid("search", None, self.search)
        if typ != "OK":
            return [], conn
        events = []
        for uid in data[0].split():
            typ, payload = conn.uid("fetch", uid, "(BODY.PEEK[])")
            if typ != "OK" or not payload or not payload[0]:
                events.append(Unreadable(uid, None))
                continue
            events.append(classify(uid, email.message_from_bytes(payload[0][1]),
                                   known_addresses, claim_unknown))
        return events, conn

    @staticmethod
    def consume(conn, uid, ok=True):
        """Marks a message read; flags it too when it did not go cleanly.

        Both halves were learnt the hard way. Leaving a failure unread re-fetched
        a dead link every tick forever. Marking it read without `\\Flagged` left
        it invisible, so a failure nobody could find looked like mail that never
        arrived.
        """
        conn.uid("store", uid, "+FLAGS", "(\\Seen)" if ok else "(\\Seen \\Flagged)")
