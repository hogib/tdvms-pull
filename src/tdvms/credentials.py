"""The mailbox credentials: from the environment, or from a file the TUI saves.

Not a runnable script -- imported only.

Three variables, looked up in this order for each one:

1. `TDVMS_IMAP_*` in the environment,
2. `AFAD_IMAP_*` in the environment (the older campaign tool's names),
3. `mailbox.env` in the config folder: `$XDG_CONFIG_HOME/tdvms`,
   `%APPDATA%\\tdvms` on Windows, else `~/.config/tdvms` (see `compat`).

The environment always wins, so an operator who exports credentials -- by hand,
from direnv, from a secrets manager -- gets exactly what they exported, and the
file only fills in what nobody set. The file is readable by its owner only
(0600, or an owner-only ACL on Windows), and in shell syntax so it can also be
`source`d.
"""
import os
import pathlib
import shlex

from tdvms import compat

VARS = ("TDVMS_IMAP_HOST", "TDVMS_IMAP_USER", "TDVMS_IMAP_PASS")
LEGACY = {"TDVMS_IMAP_HOST": "AFAD_IMAP_HOST", "TDVMS_IMAP_USER": "AFAD_IMAP_USER",
          "TDVMS_IMAP_PASS": "AFAD_IMAP_PASS"}


def path():
    return compat.config_dir() / "mailbox.env"


def read_file(p=None, keys=VARS):
    """`KEY=value` lines into a dict. Other keys and comments are ignored."""
    p = pathlib.Path(p or path())
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = line.removeprefix("export ").strip()
        key, _, raw = line.partition("=")
        if key in keys:
            parts = shlex.split(raw) if raw else [""]
            out[key] = parts[0] if parts else ""
    return out


def write_file(values, p=None, keys=VARS, what="mailbox credentials"):
    """Saves `keys` from `values`, readable by the owner only. Returns the path.

    Created 0600 from the first byte rather than chmod-ed after, so the password
    is never on disk with wider permissions, even for an instant.
    """
    p = pathlib.Path(p or path())
    p.parent.mkdir(parents=True, exist_ok=True)
    if not compat.WINDOWS:
        os.chmod(p.parent, 0o700)
    body = [f"# tdvms {what} -- written by tdvms-tui.",
            "# Environment variables of the same name take precedence."]
    body += [f"{k}={shlex.quote(values.get(k) or '')}" for k in keys]
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(body) + "\n")
    compat.replace(tmp, p)
    if compat.WINDOWS:
        # The mode above means nothing to NTFS; an ACL does.
        compat.private(p)
    return p


def resolve(environ=None, p=None):
    """`{VAR: (value, source)}`; source names where the value came from, or None."""
    environ = os.environ if environ is None else environ
    saved = read_file(p)
    out = {}
    for var in VARS:
        if environ.get(var):
            out[var] = (environ[var], f"environment ({var})")
        elif environ.get(LEGACY[var]):
            out[var] = (environ[LEGACY[var]], f"environment ({LEGACY[var]})")
        elif saved.get(var):
            out[var] = (saved[var], str(pathlib.Path(p or path())))
        else:
            out[var] = (None, None)
    return out


def check(host, user, password, timeout=20):
    """Logs in and opens INBOX. Returns a one-line description, or raises."""
    import imaplib
    conn = imaplib.IMAP4_SSL(host, timeout=timeout)
    try:
        conn.login(user, password)
        typ, data = conn.select("INBOX", readonly=True)
        if typ != "OK":
            raise RuntimeError(f"logged in, but INBOX would not open: {data}")
        return f"logged in as {user}; INBOX holds {int(data[0])} message(s)"
    finally:
        try:
            conn.logout()
        except Exception:
            pass
