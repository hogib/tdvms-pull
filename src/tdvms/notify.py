"""Telling a person when the campaign is done, or needs one.

Not a runnable script -- imported only.

A loop that runs for days is mostly ignored, which is the point -- until the
one cycle that needed someone goes by unseen. So `run` and `queue` raise a
notification when something starts needing attention, once, and say so again
when it clears:

* the mailbox has been unreadable for several cycles in a row,
* the disk is too full to download or submit,
* every slot is cooling down after a portal BUSY,
* requests are outstanding but nothing has come back for hours,
* a station was retired, or a window given up on,
* the campaign is complete.

Targets, any number of them:

* an ntfy topic URL (`https://ntfy.sh/your-topic`) or any URL that accepts a
  POST -- the body is the message, the title goes in a `Title` header;
* `desktop`, through `notify-send` (Linux) or `osascript` (macOS).

They come from `--notify`, else `TDVMS_NOTIFY` (comma-separated), else the
file `tdvms-tui` saves. A failed notification is logged and never stops the
loop.
"""
import os
import pathlib
import shutil
import subprocess

from tdvms import credentials
from tdvms.ledger import age_seconds

ENV = "TDVMS_NOTIFY"


def path():
    return credentials.path().with_name("notify.env")


def split(text):
    return [t.strip() for t in (text or "").replace(" ", ",").split(",") if t.strip()]


def targets(cli=None, environ=None, saved_path=None):
    """`--notify` if given, else the environment, else the saved file."""
    if cli:
        return list(cli)
    environ = os.environ if environ is None else environ
    if environ.get(ENV):
        return split(environ[ENV])
    return split(credentials.read_file(saved_path or path(), keys=(ENV,)).get(ENV))


class Notifier:
    def __init__(self, targets, log=print, session=None):
        self.targets = list(targets)
        self.log = log
        self.session = session

    def __bool__(self):
        return bool(self.targets)

    def send(self, title, body, urgent=False):
        """Sends to every target. Returns the failures as `[(target, error)]`."""
        failed = []
        for t in self.targets:
            try:
                if t == "desktop":
                    _desktop(title, body)
                else:
                    import requests
                    r = (self.session or requests).post(
                        t, data=body.encode(), timeout=10,
                        headers={"Title": title.encode(),
                                 "Priority": "high" if urgent else "default",
                                 "Tags": "warning" if urgent else "seismograph"})
                    r.raise_for_status()
            except Exception as e:
                failed.append((t, f"{type(e).__name__}: {e}"))
                self.log(f"  [NOTIFY] {t}: {type(e).__name__}: {e}")
        return failed


def _desktop(title, body):
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "--app-name=tdvms", title, body],
                       check=True, timeout=10)
    elif shutil.which("osascript"):
        script = f"display notification {_q(body)} with title {_q(title)}"
        subprocess.run(["osascript", "-e", script], check=True, timeout=10)
    else:
        raise RuntimeError("no notify-send or osascript on this machine")


def _q(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Watch:
    """Turns each cycle's result into notifications: once per condition."""

    def __init__(self, notifier, name, ledger, pool, stall_hours=3.0, mail_failures=3):
        self.notifier, self.name, self.ledger, self.pool = notifier, name, ledger, pool
        self.stall_hours, self.mail_failures = stall_hours, mail_failures
        self.active = set()
        self.mail_streak = 0
        # A ledger that is already complete when the loop starts was finished
        # by an earlier run; announcing it again would be noise.
        self.done = ledger.complete()

    def _raise(self, key, title, body):
        if key not in self.active:
            self.active.add(key)
            self.notifier.send(f"{self.name}: {title}", body, urgent=True)

    def _clear(self, key, title=None, body=""):
        if key in self.active:
            self.active.discard(key)
            if title:
                self.notifier.send(f"{self.name}: {title}", body)

    def after(self, result):
        if not self.notifier:
            return
        self.mail_streak = self.mail_streak + 1 if result.mail_error else 0
        if self.mail_streak >= self.mail_failures:
            self._raise("mail", "can't read the mailbox",
                        f"{self.mail_streak} cycles in a row: {result.mail_error}. "
                        f"Nothing is downloaded until it can be read -- check the "
                        f"credentials (tdvms-tui, Settings).")
        elif not result.mail_error:
            self._clear("mail", "mailbox readable again", "Downloads have resumed.")

        if result.paused:
            self._raise("disk", "disk nearly full", result.paused)
        else:
            self._clear("disk", "disk space recovered",
                        "Downloading and submitting again.")

        slots = list(self.pool.slots.values())
        if slots and all(s.remote_busy for s in slots):
            self._raise("busy", "every slot is cooling down",
                        "The portal answered BUSY to all of them. They retry on their "
                        "own; if this keeps happening, another tool may be using the "
                        "same addresses.")
        else:
            self._clear("busy")

        rows = self.ledger.rows()
        stalled = self._stall(rows)
        if stalled:
            self._raise("stall", f"nothing back for {self.stall_hours:g} h", stalled)
        else:
            self._clear("stall", "results are arriving again")

        if result.retired:
            stations = sorted({cid.split(":")[0] for cid in result.retired})
            self.notifier.send(f"{self.name}: stopped requesting {', '.join(stations)}",
                               f"{len(result.retired)} pending window(s) retired; "
                               f"see the log for why.")
        if result.rejected:
            self.notifier.send(f"{self.name}: gave up on {len(result.rejected)} window(s)",
                               ", ".join(result.rejected[:5]))

        if not self.done and rows and all(r["state"] not in ("pending", "claimed",
                                                             "submitted") for r in rows):
            self.done = True
            by = {}
            for r in rows:
                by[r["state"]] = by.get(r["state"], 0) + 1
            gb = sum(r.get("bytes") or 0 for r in rows if r["state"] == "fetched") / 1e9
            self.notifier.send(f"{self.name}: complete",
                               f"{by.get('fetched', 0)} fetched ({gb:.1f} GB), "
                               f"{by.get('nodata', 0)} no data, {by.get('failed', 0)} "
                               f"failed, {by.get('retired', 0)} retired.")

    def _stall(self, rows):
        """A sentence if requests are waiting and nothing has come back lately."""
        waiting = [age_seconds(r.get("submitted_at")) for r in rows
                   if r["state"] == "submitted"]
        waiting = [a for a in waiting if a is not None]
        limit = self.stall_hours * 3600
        if not waiting or max(waiting) < limit:
            return None
        answered = [age_seconds(r.get("fetched_at")) for r in rows
                    if r["state"] in ("fetched", "nodata")]
        answered = [a for a in answered if a is not None]
        if answered and min(answered) < limit:
            return None
        return (f"{len(waiting)} request(s) waiting, the oldest for "
                f"{max(waiting) / 3600:.1f} h. Check the mailbox and the portal.")


def save(value, p=None):
    """Stores the targets for `run` to find; what the TUI's Save does."""
    return credentials.write_file({ENV: value}, pathlib.Path(p or path()), keys=(ENV,),
                                  what="notification targets")
