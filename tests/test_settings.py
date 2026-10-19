"""Saved defaults: the slot count, the base address, and where they come from.

`--address` and `--slots` had to be typed on every run. With the mailbox and
the slot count saved once in `tdvms-tui`, `tdvms run --ledger x.jsonl` is the
whole command -- and a flag still overrides, then the environment, then the
saved file.
"""
import argparse
import json

import pytest

from conftest import chunk, write
from tdvms import run, settings, slots
from tdvms.campaign import discover, new_campaign
from tdvms.tui import model


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for var in ("TDVMS_SLOTS", "TDVMS_NOTIFY", "TDVMS_IMAP_HOST", "TDVMS_IMAP_USER",
                "TDVMS_IMAP_PASS", "AFAD_IMAP_HOST", "AFAD_IMAP_USER", "AFAD_IMAP_PASS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def test_slots_come_from_the_flag_then_the_environment_then_the_file(monkeypatch):
    assert settings.slots() == 8
    settings.save({"TDVMS_SLOTS": "15"})
    assert settings.slots() == 15
    monkeypatch.setenv("TDVMS_SLOTS", "4")
    assert settings.slots() == 4
    assert settings.slots(cli=2) == 2


def test_a_nonsense_slot_count_is_refused_and_says_where_it_came_from(monkeypatch):
    monkeypatch.setenv("TDVMS_SLOTS", "lots")
    with pytest.raises(ValueError, match="from environment"):
        settings.slots()


def test_saving_one_setting_keeps_the_others():
    settings.save({"TDVMS_NOTIFY": "https://ntfy.sh/t"})
    settings.save({"TDVMS_SLOTS": "12"})
    assert settings.get("TDVMS_NOTIFY")[0] == "https://ntfy.sh/t"
    assert settings.get("TDVMS_SLOTS")[0] == "12"


def test_the_base_address_is_the_mailbox_unless_given(monkeypatch):
    assert settings.address() is None
    monkeypatch.setenv("TDVMS_IMAP_USER", "you@gmail.com")
    assert settings.address() == "you@gmail.com"
    assert settings.address("other@gmail.com") == "other@gmail.com"


def _run_args(*extra):
    p = argparse.ArgumentParser()
    p.add_argument("--ledger")
    run.add_args(p)
    return p.parse_args(list(extra))


def test_run_needs_neither_flag_once_settings_exist(monkeypatch):
    monkeypatch.setenv("TDVMS_IMAP_USER", "you@gmail.com")
    settings.save({"TDVMS_SLOTS": "15"})
    args = _run_args()
    assert run.pool_from(args) == ("you@gmail.com", 15)
    assert (args.address, args.slots) == ("you@gmail.com", 15)


def test_run_with_no_address_anywhere_says_how_to_give_one():
    with pytest.raises(SystemExit, match="--address"):
        run.pool_from(_run_args())


# --- the lock says what a run is doing ----------------------------------------------

def test_the_tui_finds_a_run_through_its_lock_without_proc(tmp_path):
    ledger = tmp_path / "campaigns" / "van" / "ledger.jsonl"
    fh = slots.hold("you@gmail.com", ledger, command="queue", slots=15,
                    out_dir=str(tmp_path / "raw"), log=str(tmp_path / "run.log"),
                    queue=[str(ledger.resolve())])
    [r] = model.live_runs(proc=tmp_path / "no-proc-here")
    assert (r.command, r.address, r.slots, r.ledger) == ("queue", "you@gmail.com", 15,
                                                         ledger.resolve())
    assert r.log == tmp_path / "run.log" and r.queue == (ledger.resolve(),)
    fh.close()
    assert model.live_runs(proc=tmp_path / "no-proc-here") == []


def test_an_old_style_lock_is_still_named_correctly(tmp_path):
    """Runs started before locks were JSON wrote `pid N, ledger PATH`."""
    assert slots.describe("pid 7, ledger /x/y.jsonl\n") == "pid 7, ledger /x/y.jsonl"
    assert slots.describe(json.dumps({"pid": 7, "ledger": "/x/y.jsonl"})) == \
        "pid 7, ledger /x/y.jsonl"


# --- --log ------------------------------------------------------------------------

def test_log_sends_everything_to_the_file(monkeypatch, tmp_path):
    import sys
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    where = run.log_to(tmp_path / "logs" / "run.log")
    print("[12:00:00] fetched 1 → silivri_raw")
    sys.stdout.flush()
    sys.stdout.close()          # Windows will not delete a file still open
    assert where == (tmp_path / "logs" / "run.log").resolve()
    assert "fetched 1 → silivri_raw" in where.read_text(encoding="utf-8")


# --- downloads on another disk, without symlinks -------------------------------------

def test_without_symlinks_a_pointer_file_names_the_download_folder(tmp_path, monkeypatch):
    """Windows makes links only for administrators and Developer Mode."""
    import pathlib

    def refuse(self, *a, **k):
        raise OSError("A required privilege is not held by the client")
    monkeypatch.setattr(pathlib.Path, "symlink_to", refuse)
    elsewhere = tmp_path / "D-drive" / "van"
    c = new_campaign(tmp_path / "repo", "van-2011", elsewhere)
    assert c.raw == elsewhere.resolve() and elsewhere.is_dir()
    write(c.path, [chunk()])
    [found] = discover(tmp_path / "repo")
    assert found.raw == elsewhere.resolve()
    args = _run_args("--address", "you@gmail.com")
    assert run.describe(args, found.raw)["out_dir"] == str(elsewhere.resolve())


def test_the_start_command_suits_the_os(tmp_path):
    c = new_campaign(tmp_path, "van-2011")
    posix = model.run_command(c, tmp_path, "you@gmail.com", 15, windows=False)
    win = model.run_command(c, tmp_path, "you@gmail.com", 15, windows=True)
    assert posix.startswith("setsid nohup uv run tdvms run") and "--log campaigns" in posix
    assert win.startswith("Start-Process -WindowStyle Hidden uv -ArgumentList 'run tdvms run")
    assert "--slots 15" in win and "--log" in win and "setsid" not in win
