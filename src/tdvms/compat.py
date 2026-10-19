"""Everything that differs between Linux, macOS and Windows, in one place.

Not a runnable script -- imported only.

* **Where things live.** Settings in `$XDG_CONFIG_HOME/tdvms`, `%APPDATA%\\tdvms`
  or `~/.config/tdvms`; run locks in `$XDG_STATE_HOME/tdvms`,
  `%LOCALAPPDATA%\\tdvms` or `~/.local/state/tdvms`.
* **Locks.** `flock` where it exists, `msvcrt.locking` on Windows. Both are
  dropped by the OS when the holder dies, so a lock can never go stale. A
  Windows lock is *mandatory* -- nobody else can read the locked bytes -- so it
  covers one byte far past the end of the file, and the holder's description
  at the start stays readable to the process asking who has it.
* **Output.** Logs carry `→` and `—`, which Windows' default code page cannot
  encode; a redirected log is written as UTF-8 instead of crashing the loop.
* **Private files.** Owner-only permissions with `chmod` or, on Windows,
  `icacls`.
"""
import os
import pathlib
import subprocess
import sys
import time

WINDOWS = os.name == "nt"
# Past the end of any lock file's text; see the module docstring.
LOCK_OFFSET = 0x7FFFFFFF


def _base(xdg, windows, fallback):
    if os.environ.get(xdg):
        return pathlib.Path(os.environ[xdg])
    if WINDOWS and os.environ.get(windows):
        return pathlib.Path(os.environ[windows])
    return pathlib.Path.home().joinpath(*fallback)


def config_dir():
    return _base("XDG_CONFIG_HOME", "APPDATA", (".config",)) / "tdvms"


def state_dir():
    return _base("XDG_STATE_HOME", "LOCALAPPDATA", (".local", "state")) / "tdvms"


# --- locks ------------------------------------------------------------------------

def lock(fh, block=True):
    """Exclusive lock on an open file.

    Raises:
        BlockingIOError: `block=False` and another process holds it.
    """
    if WINDOWS:
        import msvcrt
        while True:
            try:
                _at_offset(fh, lambda fd: msvcrt.locking(fd, msvcrt.LK_NBLCK, 1))
                return
            except OSError:
                if not block:
                    raise BlockingIOError("locked by another process") from None
                time.sleep(0.05)
    import fcntl
    fcntl.flock(fh, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))


def unlock(fh):
    if WINDOWS:
        import msvcrt
        _at_offset(fh, lambda fd: msvcrt.locking(fd, msvcrt.LK_UNLCK, 1))
        return
    import fcntl
    fcntl.flock(fh, fcntl.LOCK_UN)


def held(fh):
    """Whether another process holds the lock on this file. Takes nothing."""
    try:
        lock(fh, block=False)
    except BlockingIOError:
        return True
    unlock(fh)
    return False


def _at_offset(fh, act):
    """Runs `act(fd)` with the file positioned at `LOCK_OFFSET`, then puts it back."""
    fh.flush()
    fd = fh.fileno()
    pos = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
    try:
        act(fd)
    finally:
        os.lseek(fd, pos, os.SEEK_SET)


# --- replacing files ---------------------------------------------------------------

def replace(src, dst, attempts=50, pause=0.02):
    """`os.replace`, patient on Windows.

    Windows refuses to replace a file another process has open, even for a
    moment -- and the TUI reads the ledger every two seconds while `run`
    rewrites it. Unanswered, that collision would stop the loop; it clears
    within milliseconds, so it is retried for up to a second.
    """
    for attempt in range(attempts):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if not WINDOWS or attempt == attempts - 1:
                raise
            time.sleep(pause)


# --- output -----------------------------------------------------------------------

def utf8_output():
    """Never let a character the console cannot show stop the program.

    A redirected log is UTF-8 whatever the locale says; a console keeps its own
    encoding and shows `?` for what it cannot.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def stdout_path():
    """The file this process's output goes to, where the OS can say; else None.

    Linux knows through `/proc`. Elsewhere `run --log FILE` records it instead.
    """
    try:
        p = pathlib.Path(os.readlink("/proc/self/fd/1"))
    except (OSError, AttributeError, NotImplementedError):
        return None
    return p if p.is_absolute() and p.is_file() else None


# --- private files ----------------------------------------------------------------

def private(path):
    """Readable and writable by the current user only."""
    if WINDOWS:
        user = os.environ.get("USERNAME")
        if user:
            subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                           check=True, capture_output=True)
        return
    os.chmod(path, 0o600)
