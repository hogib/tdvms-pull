"""The TUI's model, the shipped catalogs, and the two screens driven headless.

The model is plain Python, tested without a terminal. The screens are driven
through Textual's own pilot and skipped where the `tui` extra is not installed,
because `run` must keep working on a box that never installs it.
"""
import asyncio
import json
import os
import zipfile
from datetime import date

import pytest

from conftest import chunk, write
from tdvms import catalog
from tdvms.tui import model


# --- catalogs -----------------------------------------------------------------

def test_the_shipped_station_catalog_covers_every_network():
    stations = catalog.load_stations()
    assert {s.network for s in stations} == set(catalog.NETWORKS)
    assert len(stations) > 1500


def test_a_code_listed_in_two_networks_is_two_stations():
    """ALT, KULU and MADM are each listed twice. Keyed by code alone, picking
    one on the map would silently pick the other."""
    madm = [s for s in catalog.load_stations() if s.code == "MADM"]
    assert len(madm) == 2 and len({s.key for s in madm}) == 2


def test_distance_agrees_with_the_pair_the_client_was_fixed_for():
    """CTKS is the station `client.py` names as 11.9 km from ELBA."""
    by = {s.key: s for s in catalog.load_stations()}
    elba, ctks = by["TU.ELBA"], by["KO.CTKS"]
    km = catalog.distance_km(elba.latitude, elba.longitude, ctks.latitude, ctks.longitude)
    assert 11 < km < 13


def test_the_shipped_event_catalog_holds_the_events_campaigns_are_built_on():
    big = {(e.date, e.magnitude) for e in catalog.load_events()}
    assert ("2023-02-06", 7.8) in big          # Kahramanmaraş
    assert ("2011-10-23", 7.1) in big          # Van


# --- discovery ----------------------------------------------------------------

def test_both_layouts_are_found_and_the_cli_default_downloads_to_afad_raw(tmp_path):
    write(tmp_path / "van_ledger.jsonl", [chunk()])
    write(tmp_path / "tdvms_ledger.jsonl", [chunk()])
    (tmp_path / "campaigns" / "kahta").mkdir(parents=True)
    write(tmp_path / "campaigns" / "kahta" / "ledger.jsonl", [chunk()])
    found = {c.name: c for c in model.discover(tmp_path)}
    assert set(found) == {"van", "tdvms", "kahta"}
    assert found["van"].raw == tmp_path / "van_raw"
    assert found["tdvms"].raw == tmp_path / "afad_raw"
    assert found["kahta"].raw == tmp_path / "campaigns" / "kahta" / "raw"


def test_a_new_campaign_is_a_folder_with_its_downloads_beside_it(tmp_path):
    c = model.new_campaign(tmp_path, "van-2011")
    assert c.raw.is_dir() and not c.raw.is_symlink()
    assert c.path == tmp_path / "campaigns" / "van-2011" / "ledger.jsonl"


def test_downloads_on_another_disk_are_linked_in_not_moved(tmp_path):
    elsewhere = tmp_path / "data-disk" / "van"
    c = model.new_campaign(tmp_path / "repo", "van-2011", elsewhere)
    assert c.raw.is_symlink() and c.raw.resolve() == elsewhere.resolve()
    assert elsewhere.is_dir()


@pytest.mark.parametrize("name", ["Van", "", "../up", "a b"])
def test_a_campaign_name_that_is_not_a_plain_slug_is_refused(tmp_path, name):
    with pytest.raises(ValueError):
        model.new_campaign(tmp_path, name)


def test_a_campaign_name_already_taken_by_a_flat_ledger_is_refused(tmp_path):
    write(tmp_path / "van_ledger.jsonl", [chunk()])
    with pytest.raises(ValueError, match="already exists"):
        model.new_campaign(tmp_path, "van")


# --- live runs ----------------------------------------------------------------

@pytest.mark.parametrize("argv, args", [
    (["uv", "run", "tdvms", "run", "--ledger", "x"], None),
    ([".venv/bin/python", ".venv/bin/tdvms", "run", "--ledger", "x"], ["--ledger", "x"]),
    (["python", "-m", "tdvms.run", "--slots=4"], ["--slots=4"]),
    ([".venv/bin/python", ".venv/bin/tdvms", "status"], None),
])
def test_the_launcher_is_skipped_so_a_run_is_counted_once(argv, args):
    assert model.run_args(argv) == args


def test_a_run_is_read_from_proc_with_its_ledger_resolved_in_its_own_cwd(tmp_path):
    proc, cwd = tmp_path / "proc", tmp_path / "work"
    (proc / "4242" / "fd").mkdir(parents=True)
    cwd.mkdir()
    log = cwd / "silivri_run.log"
    log.write_text("")
    (proc / "4242" / "cmdline").write_bytes(b"\0".join(
        [b"/v/bin/python", b"/v/bin/tdvms", b"run", b"--ledger", b"silivri_ledger.jsonl",
         b"--address", b"you@gmail.com", b"--slots", b"15", b"--out-dir", b"silivri_raw"]))
    (proc / "4242" / "cwd").symlink_to(cwd)
    (proc / "4242" / "fd" / "1").symlink_to(log)
    (proc / "self").mkdir()
    [run] = model.live_runs(proc)
    assert (run.pid, run.address, run.slots, run.out_dir) == (4242, "you@gmail.com", 15,
                                                               "silivri_raw")
    assert run.ledger == (cwd / "silivri_ledger.jsonl").resolve()
    assert run.log == log


def test_a_pool_is_guessed_from_the_addresses_a_ledger_used(ledger_path):
    led = write(ledger_path, [chunk(start="2025-01-01T00:00:00", email="you+a3@gmail.com"),
                              chunk(start="2025-02-01T00:00:00", email="you+a11@gmail.com")])
    assert model.infer_pool(led.rows()) == ("you@gmail.com", 11)


# --- planning -----------------------------------------------------------------

def test_the_preview_counts_exactly_what_the_plan_then_adds(tmp_path):
    c = model.new_campaign(tmp_path, "t")
    model.apply_plan(c, ["ELBA"], "2025-01-01", "2025-03-01", 21)
    total, new = model.preview(c.ledger.rows(), ["ELBA", "CTKS"], "2025-01-01",
                               "2025-04-01", 21)
    assert (total, new) == (10, 7)
    assert model.apply_plan(c, ["ELBA", "CTKS"], "2025-01-01", "2025-04-01", 21) == new
    assert model.apply_plan(c, ["ELBA", "CTKS"], "2025-01-01", "2025-04-01", 21) == 0


def test_a_month_is_moved_by_calendar_and_clamped():
    assert model.add_months(date(2024, 3, 31), -1) == date(2024, 2, 29)
    assert model.add_months(date(2020, 1, 24), -6) == date(2019, 7, 24)
    assert model.add_months(date(2020, 1, 24), 12) == date(2021, 1, 24)


@pytest.mark.parametrize("place, year, name", [
    ("27 km NNE of Van, Turkey", "2011", "van-2011"),
    ("Pazarcik earthquake, Kahramanmaras earthquake sequence", "2023", "pazarcik-2023"),
    ("8 km S of Düzce, Turkey", "1999", "duzce-1999"),
])
def test_an_event_suggests_a_campaign_name(place, year, name):
    e = catalog.Event("x", f"{year}-01-01T00:00:00", 7.0, 0, 0, 10, place)
    assert model.suggest_name(e) == name


# --- strays -------------------------------------------------------------------

MEMBER = "KO_SLVT_26032019_000000_16042019_000000_HH.mseed"


def _stray(tmp_path, state="submitted"):
    """A silivri window downloaded by balikesir's run, as on 2026-10-19."""
    (tmp_path / "balikesir_raw").mkdir()
    write(tmp_path / "balikesir_ledger.jsonl", [chunk("DEMI", state="fetched")])
    write(tmp_path / "silivri_ledger.jsonl",
          [chunk("SLVT", start="2019-03-26T00:00:00", state=state,
                 email="you+a5@gmail.com")])
    path = tmp_path / "balikesir_raw" / f"UNMATCHED_{MEMBER}.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(MEMBER, b"\0" * 4096)
    return path


def test_a_stray_is_matched_to_the_ledger_that_holds_its_window(tmp_path):
    _stray(tmp_path)
    [s] = model.strays(model.discover(tmp_path))
    assert (s.campaign.name, s.status) == ("silivri", "file")


def test_filing_a_stray_records_the_fetch_and_frees_the_slot(tmp_path):
    path = _stray(tmp_path)
    [s] = model.strays(model.discover(tmp_path))
    model.file_stray(s)
    [row] = s.campaign.ledger.rows()
    assert row["state"] == "fetched" and "recovered" in row["note"]
    assert (tmp_path / "silivri_raw" / "SLVT" / "SLVT_2019-03-26.zip").exists()
    assert not path.exists()


def test_a_stray_on_another_disk_is_copied_before_it_is_filed(tmp_path, monkeypatch):
    """`file_archive` renames, which cannot cross filesystems -- and the raw
    folders sit on different disks. Filed by `ingest` as-is, every one of the
    eleven strays would have failed with EXDEV."""
    path = _stray(tmp_path)
    (tmp_path / "silivri_raw").mkdir()
    real = os.stat

    def stat(p, *a, **kw):
        st = real(p, *a, **kw)
        if os.fspath(p) == os.fspath(path):
            fields = list(st[:10])
            fields[2] += 1                     # st_dev: another disk
            return os.stat_result(fields, {k: getattr(st, k) for k in (
                "st_atime_ns", "st_mtime_ns", "st_ctime_ns")})
        return st
    monkeypatch.setattr(os, "stat", stat)
    renamed = []
    original = type(path).replace

    def replace(self, target):
        renamed.append(self)
        return original(self, target)
    monkeypatch.setattr(type(path), "replace", replace)

    [s] = model.strays(model.discover(tmp_path))
    model.file_stray(s)
    assert path not in renamed                  # the original is never renamed
    assert not path.exists()
    assert s.campaign.ledger.rows()[0]["state"] == "fetched"


def test_a_duplicate_can_be_deleted_and_nothing_else_can(tmp_path):
    path = _stray(tmp_path, state="fetched")
    [s] = model.strays(model.discover(tmp_path))
    assert s.status == "duplicate"
    with pytest.raises(model.RepairError):
        model.file_stray(s)
    model.delete_stray(s)
    assert not path.exists()


def test_an_orphan_is_neither_filed_nor_deleted(tmp_path):
    path = _stray(tmp_path)
    (tmp_path / "silivri_ledger.jsonl").unlink()
    [s] = model.strays(model.discover(tmp_path))
    assert s.status == "orphan"
    for act in (model.file_stray, model.delete_stray):
        with pytest.raises(model.RepairError):
            act(s)
    assert path.exists()


def test_a_log_tail_starts_on_a_whole_line_and_follows_growth(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("".join(f"line {i}\n" for i in range(100)))
    text, off = model.tail(log, None, keep=30)
    assert text.splitlines()[0].startswith("line ") and text.endswith("line 99\n")
    with open(log, "a") as fh:
        fh.write("line 100\n")
    assert model.tail(log, off) == ("line 100\n", off + 9)


# --- the screens ----------------------------------------------------------------

def _drive(root, script):
    pytest.importorskip("textual")
    from tdvms.tui.app import TdvmsApp

    async def go():
        app = TdvmsApp(root)
        async with app.run_test(size=(180, 50)) as pilot:
            await pilot.pause(0.3)
            await script(app, pilot)
    asyncio.run(go())


def test_earthquake_mode_plans_a_new_campaign_end_to_end(tmp_path):
    from_ = {}

    async def script(app, pilot):
        from textual.widgets import DataTable, Input
        await pilot.press("f2")
        await pilot.pause(0.3)
        await pilot.press("e")
        await pilot.pause(0.2)
        app.screen.query_one("#eq-search", Input).value = "van m7"
        await pilot.pause(0.2)
        app.screen.query_one("#events", DataTable).focus()
        await pilot.click("#apply")
        await pilot.pause(0.3)
        from tdvms.tui.app import PlanView
        pv = app.query_one(PlanView)
        from_["codes"] = sorted({s.code for s in pv.picked.values()})
        from_["start"] = pv.query_one("#start", Input).value
        await pilot.click("#go")
        await pilot.pause(0.2)
        await pilot.press("y")
        await pilot.pause(0.3)

    _drive(tmp_path, script)
    assert "TVAN" in from_["codes"] and from_["start"] == "2011-04-23"
    rows = [json.loads(l) for l in
            (tmp_path / "campaigns" / "van-2011" / "ledger.jsonl").read_text().splitlines()]
    assert {r["station"] for r in rows} == set(from_["codes"])
    assert (tmp_path / "campaigns" / "van-2011" / "raw").is_dir()


def test_a_chunk_is_reset_from_the_chunks_tab_only_after_confirming(tmp_path):
    write(tmp_path / "van_ledger.jsonl",
          [chunk("TVAN", state="nodata", fetched_at="2026-10-01T00:00:00+00:00")])

    async def script(app, pilot):
        app.query_one("#detail").active = "t-chunks"
        await pilot.pause(0.3)
        app.query_one("#chunks").focus()
        await pilot.press("r")
        await pilot.pause(0.2)
        await pilot.press("n")
        await pilot.pause(0.2)
        assert json.loads((tmp_path / "van_ledger.jsonl").read_text())["state"] == "nodata"
        await pilot.press("r")
        await pilot.pause(0.2)
        await pilot.press("y")
        await pilot.pause(0.2)

    _drive(tmp_path, script)
    row = json.loads((tmp_path / "van_ledger.jsonl").read_text())
    assert row["state"] == "pending" and row["fetched_at"] is None


def test_a_system_without_proc_has_no_live_runs_rather_than_a_crash(tmp_path):
    assert model.live_runs(tmp_path / "no-such-proc") == []


def test_the_mailbox_tab_saves_what_is_typed_and_shows_each_source(monkeypatch,
                                                                   tmp_path):
    pytest.importorskip("textual")
    from textual.widgets import Input, Static

    from tdvms import credentials
    from tdvms.tui.app import TdvmsApp
    for var in credentials.VARS + tuple(credentials.LEGACY.values()):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("TDVMS_IMAP_HOST", "imap.example.org")
    tricky = """p@ss w'o"rd $HOME `x`"""

    async def go():
        app = TdvmsApp(tmp_path)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("f3")
            await pilot.pause(0.3)
            app.query_one("#v-TDVMS_IMAP_USER", Input).value = "you@gmail.com"
            app.query_one("#v-TDVMS_IMAP_PASS", Input).value = tricky
            await pilot.click("#save")
            await pilot.pause(0.3)
            host = str(app.query_one("#s-TDVMS_IMAP_HOST", Static).render())
            user = str(app.query_one("#s-TDVMS_IMAP_USER", Static).render())
            return host, user

    host, user = asyncio.run(go())
    assert "environment" in host and "saved in" in user
    assert credentials.read_file() == {"TDVMS_IMAP_HOST": "imap.example.org",
                                       "TDVMS_IMAP_USER": "you@gmail.com",
                                       "TDVMS_IMAP_PASS": tricky}

# --- a running queue, and the coverage tab -------------------------------------

def _campaign_dir(root, name, *rows):
    home = root / "campaigns" / name
    home.mkdir(parents=True)
    write(home / "ledger.jsonl", list(rows))
    return home / "ledger.jsonl"


def test_the_tui_reads_a_queue_and_knows_which_campaign_it_is_on(tmp_path):
    done = _campaign_dir(tmp_path, "old", chunk(state="fetched"))
    now_on = _campaign_dir(tmp_path, "first", chunk())
    later = _campaign_dir(tmp_path, "second", chunk())
    proc = tmp_path / "proc"
    (proc / "77" / "fd").mkdir(parents=True)
    (proc / "77" / "cmdline").write_bytes(b"\0".join(
        [b"/v/bin/python", b"/v/bin/tdvms", b"queue", b"--claim-unknown", b"old",
         b"first", b"second", b"--address", b"you@gmail.com", b"--slots", b"4"]))
    (proc / "77" / "cwd").symlink_to(tmp_path)
    [r] = model.live_runs(proc)
    assert (r.command, r.ledger, r.slots) == ("queue", now_on.resolve(), 4)
    assert r.queue == (done.resolve(), now_on.resolve(), later.resolve())
    second = next(c for c in model.discover(tmp_path) if c.name == "second")
    assert model.queued_for(second, [r]) == (r, 1)
    assert model.live_for(second, [r]) is None


def test_a_flag_never_swallows_the_word_after_it():
    opts, rest = model.parse(["--claim-unknown", "silivri", "--slots", "3", "van"])
    assert rest == ["silivri", "van"] and opts == {"claim-unknown": "", "slots": "3"}


def test_the_coverage_tab_shows_each_channel(tmp_path):
    pytest.importorskip("textual")
    from textual.widgets import DataTable

    from tdvms.tui.app import TdvmsApp
    from test_coverage import _campaign, stream
    _campaign(tmp_path, stream(*range(0, 600)))

    async def go():
        app = TdvmsApp(tmp_path)
        async with app.run_test(size=(180, 40)) as pilot:
            app.query_one("#detail").active = "t-coverage"
            for _ in range(30):
                await pilot.pause(0.1)
                if app.query_one("#coverage", DataTable).row_count:
                    break
            t = app.query_one("#coverage", DataTable)
            return [str(c) for c in t.get_row_at(0)]
    row = asyncio.run(go())
    assert row[0] == "ELBA" and row[1] == "HHZ"
