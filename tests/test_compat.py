"""What differs on Windows, tested on any OS.

The suite runs on Linux, so the Windows branches are driven through stand-ins:
an `msvcrt` that enforces locks the way Windows does, and a recorded
`subprocess.run`. What they pin is the part that is easy to get wrong -- where
the lock sits, what is passed to `icacls` and PowerShell -- not Windows itself.
"""
import io
import os
import sys
import types

import pytest

from tdvms import compat, notify


# --- where things live --------------------------------------------------------------

def test_xdg_wins_then_windows_folders_then_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert compat.config_dir() == tmp_path / "xdg" / "tdvms"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(compat, "WINDOWS", True)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert compat.config_dir() == tmp_path / "Roaming" / "tdvms"
    assert compat.state_dir() == tmp_path / "Local" / "tdvms"


# --- locks --------------------------------------------------------------------------

def test_a_lock_is_seen_by_another_handle_and_released(tmp_path):
    path = tmp_path / "x.lock"
    a, b = open(path, "a+"), open(path, "a+")
    compat.lock(a, block=False)
    assert compat.held(b)
    with pytest.raises(BlockingIOError):
        compat.lock(b, block=False)
    compat.unlock(a)
    assert not compat.held(b)
    a.close()
    b.close()


class FakeMsvcrt(types.ModuleType):
    """Windows byte-range locks: mandatory, per handle, at the file position."""
    LK_NBLCK, LK_UNLCK = 2, 0

    def __init__(self):
        super().__init__("msvcrt")
        self.locks = {}          # (inode, offset) -> fd holding it

    def locking(self, fd, mode, nbytes):
        key = (os.fstat(fd).st_ino, os.lseek(fd, 0, os.SEEK_CUR))
        if mode == self.LK_UNLCK:
            self.locks.pop(key, None)
        elif self.locks.get(key, fd) != fd:
            raise PermissionError(13, "Permission denied")
        else:
            self.locks[key] = fd


@pytest.fixture
def windows(monkeypatch):
    fake = FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(compat, "WINDOWS", True)
    return fake


def test_on_windows_the_lock_sits_past_the_text_so_the_holder_stays_readable(tmp_path, windows):
    """A Windows lock is mandatory: locking byte 0 would stop anyone reading
    who holds the pool. The lock goes far past the end; the text stays open."""
    path = tmp_path / "you@gmail.com.lock"
    a = open(path, "a+")
    a.write('{"pid": 1}\n')
    compat.lock(a, block=False)
    [(_, offset)] = windows.locks
    assert offset == compat.LOCK_OFFSET
    b = open(path)
    assert compat.held(b) and b.read() == '{"pid": 1}\n'
    compat.unlock(a)
    assert not compat.held(b)
    a.close()
    b.close()


def test_on_windows_locking_leaves_the_file_where_it_was(tmp_path, windows):
    a = open(tmp_path / "x.lock", "a+")
    a.write("hello")
    before = a.tell()
    compat.lock(a, block=False)
    assert a.tell() == before
    compat.unlock(a)
    a.close()


def test_on_windows_a_held_lock_refuses_without_blocking(tmp_path, windows):
    a, b = open(tmp_path / "x.lock", "a+"), open(tmp_path / "x.lock", "a+")
    compat.lock(a)
    with pytest.raises(BlockingIOError):
        compat.lock(b, block=False)
    compat.unlock(a)
    a.close()
    b.close()


# --- output -------------------------------------------------------------------------

def test_a_redirected_log_is_utf8_whatever_the_locale(monkeypatch):
    """Windows writes redirected output in its ANSI code page, which has no →;
    the coverage report's gap lines would have crashed it."""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    compat.utf8_output()
    print("2025-01-01 → 2025-01-02 — gap", file=stream)
    stream.flush()
    assert raw.getvalue().decode("utf-8") == "2025-01-01 → 2025-01-02 — gap\n"


# --- private files ------------------------------------------------------------------

def test_on_windows_a_private_file_gets_an_owner_only_acl(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(compat, "WINDOWS", True)
    monkeypatch.setenv("USERNAME", "oguz")
    monkeypatch.setattr(compat.subprocess, "run", lambda args, **kw: calls.append(args))
    compat.private(tmp_path / "mailbox.env")
    assert calls == [["icacls", str(tmp_path / "mailbox.env"), "/inheritance:r",
                      "/grant:r", "oguz:F"]]


# --- desktop notifications --------------------------------------------------------

def test_on_windows_the_notification_text_never_becomes_script(monkeypatch):
    """`powershell -Command` appends trailing arguments to the script, so a
    title passed that way would run as code. It travels in the environment."""
    seen = {}
    monkeypatch.setattr(compat, "WINDOWS", True)
    monkeypatch.setattr(notify.shutil, "which", lambda name: "C:/ps/powershell.exe")

    def run(args, env=None, **kw):
        seen["args"], seen["env"] = args, env
    monkeypatch.setattr(notify.subprocess, "run", run)
    title = "van: complete'; Remove-Item C:\\ -Recurse; '"
    notify._desktop(title, "4 fetched")
    assert all(title not in a for a in seen["args"])
    assert seen["env"]["TDVMS_NOTE_TITLE"] == title
    assert seen["env"]["TDVMS_NOTE_BODY"] == "4 fetched"


# --- replacing a file someone is reading ------------------------------------------

def test_on_windows_a_replace_blocked_by_a_reader_is_retried(monkeypatch, tmp_path):
    """The TUI re-reads the ledger every two seconds; on Windows a reader
    holding it open makes `run`'s rewrite fail. Retried, not fatal."""
    monkeypatch.setattr(compat, "WINDOWS", True)
    real, refusals = os.replace, [PermissionError(5, "Access is denied")] * 3

    def replace(src, dst):
        if refusals:
            raise refusals.pop()
        return real(src, dst)
    monkeypatch.setattr(compat.os, "replace", replace)
    (tmp_path / "new").write_text("rows")
    compat.replace(tmp_path / "new", tmp_path / "ledger.jsonl", pause=0)
    assert (tmp_path / "ledger.jsonl").read_text() == "rows"


def test_elsewhere_a_refused_replace_is_a_real_error(monkeypatch, tmp_path):
    def refuse(src, dst):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(compat.os, "replace", refuse)
    with pytest.raises(PermissionError):
        compat.replace(tmp_path / "a", tmp_path / "b")
