"""Mailbox credentials: the environment first, then the file the TUI saves.

The file exists so someone who has never exported a variable can still run a
campaign. It must never shadow what an operator did export, and it holds a
password, so it is never readable by anyone else -- not even for an instant.
"""
import shutil
import stat
import subprocess

import pytest

from tdvms import compat, credentials
from tdvms.mailbox import Mailbox

TRICKY = """p@ss w'o"rd $HOME `x`"""


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for var in credentials.VARS + tuple(credentials.LEGACY.values()):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def save(**kw):
    values = {"TDVMS_IMAP_HOST": "imap.gmail.com", "TDVMS_IMAP_USER": "you@gmail.com",
              "TDVMS_IMAP_PASS": TRICKY}
    values.update(kw)
    return credentials.write_file(values)


@pytest.mark.skipif(compat.WINDOWS, reason="Windows uses an ACL; see test_compat")
def test_the_file_is_private_from_the_first_byte():
    p = save()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700


def test_a_password_with_quotes_and_dollars_survives_the_round_trip():
    save()
    assert credentials.read_file()["TDVMS_IMAP_PASS"] == TRICKY


@pytest.mark.skipif(compat.WINDOWS or not shutil.which("bash"), reason="needs bash")
def test_the_file_can_be_sourced_by_a_shell_and_means_the_same_thing():
    p = save()
    out = subprocess.run(["bash", "-c", f'. "{p}"; printf %s "$TDVMS_IMAP_PASS"'],
                         capture_output=True, text=True, check=True).stdout
    assert out == TRICKY


def test_the_environment_wins_over_the_file(monkeypatch):
    save(TDVMS_IMAP_USER="saved@gmail.com")
    monkeypatch.setenv("TDVMS_IMAP_USER", "exported@gmail.com")
    got = credentials.resolve()
    assert got["TDVMS_IMAP_USER"] == ("exported@gmail.com", "environment (TDVMS_IMAP_USER)")
    assert got["TDVMS_IMAP_HOST"][1] == str(credentials.path())


def test_the_older_variable_names_still_count_as_the_environment(monkeypatch):
    save()
    monkeypatch.setenv("AFAD_IMAP_PASS", "legacy")
    assert credentials.resolve()["TDVMS_IMAP_PASS"] == ("legacy",
                                                         "environment (AFAD_IMAP_PASS)")


def test_run_reads_the_saved_file_when_nothing_is_exported():
    save()
    assert Mailbox._credentials() == ("imap.gmail.com", "you@gmail.com", TRICKY)


def test_with_nothing_anywhere_run_says_where_to_put_it():
    with pytest.raises(SystemExit, match="tdvms-tui"):
        Mailbox._credentials()

