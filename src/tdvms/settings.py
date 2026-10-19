"""Defaults `tdvms-tui` saves for the command line: slots and notifications.

Not a runnable script -- imported only.

| variable       | what                                        | when unset |
|----------------|---------------------------------------------|------------|
| `TDVMS_SLOTS`  | how many plus-addresses `run` and `queue` use | 8          |
| `TDVMS_NOTIFY` | where notifications go (see `notify`)       | nowhere    |

Each is looked up on the command line first, then in the environment, then in
`settings.env` in the config folder (see `compat.config_dir`). Nothing secret
lives here; the mailbox password has its own file (`credentials`).
"""
import os

from tdvms import compat, credentials

SLOTS, NOTIFY = "TDVMS_SLOTS", "TDVMS_NOTIFY"
KEYS = (SLOTS, NOTIFY)
DEFAULT_SLOTS = 8


def path():
    return compat.config_dir() / "settings.env"


def get(key, environ=None, p=None):
    """`(value, source)`: from the environment, else the saved file, else `(None, None)`."""
    environ = os.environ if environ is None else environ
    if environ.get(key):
        return environ[key], f"environment ({key})"
    saved = credentials.read_file(p or path(), keys=KEYS).get(key)
    if saved:
        return saved, str(p or path())
    return None, None


def save(values, p=None):
    """Merges `values` into the saved file, keeping what it does not mention."""
    p = p or path()
    current = credentials.read_file(p, keys=KEYS)
    current.update({k: v for k, v in values.items() if k in KEYS})
    return credentials.write_file(current, p, keys=KEYS, what="settings")


def slots(cli=None, environ=None, p=None):
    """The pool size: `--slots`, else `TDVMS_SLOTS`, else saved, else 8.

    Raises:
        ValueError: A saved or exported value that is not a whole number >= 1.
    """
    if cli:
        return cli
    value, source = get(SLOTS, environ, p)
    if value is None:
        return DEFAULT_SLOTS
    try:
        n = int(value)
    except ValueError:
        n = 0
    if n < 1:
        raise ValueError(f"{SLOTS}={value!r} (from {source}) is not a whole number of "
                         f"slots, 1 or more")
    return n


def address(cli=None, environ=None):
    """The base address: `--address`, else the mailbox address it reads from.

    The portal's mail for every `you+aN@...` slot arrives in `you@...`, the
    same inbox the mailbox settings log in to -- so unless the two differ on
    purpose, the mailbox address is the base.
    """
    if cli:
        return cli
    value, _ = credentials.resolve(environ)["TDVMS_IMAP_USER"]
    return value
