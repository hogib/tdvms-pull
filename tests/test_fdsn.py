"""`tdvms fdsn`: whole days from an FDSN service, and the TUI tab that watches it.

The service is faked at the session: what matters here is what each answer
leaves in the FDSN ledger and on disk, not HTTP.
"""
import asyncio
import json
from datetime import date, datetime, timedelta

import pytest
import requests

from conftest import chunk, write
from tdvms import campaign, compat, fdsn
from tdvms.ledger import Ledger
from tdvms.tui import model
from test_coverage import record

DAY = date(2016, 3, 1)


def day_bytes():
    """Ten hours of each channel from midnight: one record each, at 1 sample/s."""
    return b"".join(record(datetime(2016, 3, 1), nsamp=36000, rate=1, cha=cha,
                           sta="SVRC", net="KO") for cha in ("BHE", "BHN", "BHZ"))


class Answer:
    def __init__(self, status, content=b""):
        self.status_code, self.content = status, content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class Service:
    """Answers each GET from a script: an Answer, or an exception to raise."""

    def __init__(self, *script):
        self.script, self.asked = list(script), []

    def get(self, url, params, timeout):
        self.asked.append(params["starttime"][:10])
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def pull(tmp_path):
    write(tmp_path / "sivrice_ledger.jsonl", [chunk("SVRC")])
    c = campaign.for_ledger(tmp_path / "sivrice_ledger.jsonl")
    return c, Ledger(fdsn.state_path(c)), fdsn.default_out(c)


def test_a_pull_lives_beside_its_campaign_in_either_layout(tmp_path):
    flat = campaign.for_ledger(tmp_path / "sivrice_ledger.jsonl")
    assert fdsn.state_path(flat) == tmp_path / "sivrice_fdsn.jsonl"
    assert fdsn.default_out(flat) == tmp_path / "sivrice_fdsn_raw"
    folder = campaign.for_ledger(tmp_path / "campaigns" / "van" / "ledger.jsonl")
    assert fdsn.state_path(folder) == tmp_path / "campaigns" / "van" / "fdsn.jsonl"


def test_planning_twice_adds_each_day_once(pull):
    _, ledger, _ = pull
    assert fdsn.plan(ledger, "SVRC", [DAY, DAY], "KO", "BH?", fdsn.KOERI) == 1
    assert fdsn.plan(ledger, "SVRC", [DAY, date(2016, 3, 2)], "KO", "BH?", fdsn.KOERI) == 1
    row = ledger.rows()[0]
    assert (row["start"], row["end"], row["state"]) == \
        ("2016-03-01T00:00:00", "2016-03-02T00:00:00", "pending")
    assert (row["network"], row["channels"], row["service"]) == ("KO", "BH?", fdsn.KOERI)


def test_a_day_with_data_is_filed_under_the_name_tdvms_gives_its_own(pull):
    _, ledger, out = pull
    fdsn.plan(ledger, "SVRC", [DAY], "KO", "BH?", fdsn.KOERI)
    counts = fdsn.work(ledger, out, session=Service(Answer(200, day_bytes())), log=str)
    assert counts == {"fetched": 1}
    row = ledger.rows()[0]
    path = out / "SVRC" / "KO_SVRC_01032016_000000_02032016_000000_BH.mseed"
    assert row["path"] == str(path) and path.read_bytes() == day_bytes()
    assert row["hours"] == {"BHE": 10.0, "BHN": 10.0, "BHZ": 10.0}
    assert not list(out.rglob("*.part"))


def test_hours_count_only_what_falls_inside_the_day():
    late = record(datetime(2016, 3, 1, 20), nsamp=36000, rate=1, cha="BHN",
                  sta="SVRC", net="KO")          # 20:00 for 10 h: 4 h of them on the day
    assert fdsn.day_hours(late, DAY) == {"BHN": 4.0}


def test_no_data_at_source_is_an_outcome_not_a_failure(pull):
    _, ledger, out = pull
    fdsn.plan(ledger, "SVRC", [DAY], "KO", "BH?", fdsn.KOERI)
    assert fdsn.work(ledger, out, session=Service(Answer(404)), log=str) == {"nodata": 1}
    assert not out.exists()


def test_a_reset_connection_is_retried_and_then_recorded_failed(pull):
    _, ledger, out = pull
    fdsn.plan(ledger, "SVRC", [DAY, date(2016, 3, 2)], "KO", "BH?", fdsn.KOERI)
    service = Service(requests.ConnectionError("reset"), Answer(200, day_bytes()),
                      *[requests.ConnectionError("reset")] * 3)
    counts = fdsn.work(ledger, out, retries=3, pause=0, session=service, log=str)
    assert counts == {"fetched": 1, "failed": 1}
    assert service.asked == ["2016-03-01"] * 2 + ["2016-03-02"] * 3
    failed = next(r for r in ledger.rows() if r["state"] == "failed")
    assert "ConnectionError" in failed["note"]


def test_a_day_already_on_disk_is_counted_not_fetched_again(pull):
    _, ledger, out = pull
    fdsn.plan(ledger, "SVRC", [DAY], "KO", "BH?", fdsn.KOERI)
    path = out / "SVRC" / fdsn.file_name("KO", "SVRC", DAY, "BH?")
    path.parent.mkdir(parents=True)
    path.write_bytes(day_bytes())
    assert fdsn.adopt(ledger, out) == 1
    row = ledger.rows()[0]
    assert row["state"] == "fetched" and row["note"] == fdsn.ADOPTED
    assert fdsn.work(ledger, out, session=Service(), log=str) == {}


def test_a_day_left_claimed_by_a_dead_pull_goes_back_to_pending(pull):
    _, ledger, _ = pull
    fdsn.plan(ledger, "SVRC", [DAY], "KO", "BH?", fdsn.KOERI)
    ledger.update("SVRC:2016-03-01T00:00:00", state="claimed", claimed_at="x")
    assert fdsn.reclaim(ledger) == 1
    assert ledger.rows()[0]["state"] == "pending"


def test_the_run_lock_says_who_holds_it_and_nothing_once_released(pull):
    c, ledger, _ = pull
    state = fdsn.state_path(c)
    assert fdsn.holder(state) is None
    fh = open(fdsn.run_lock(state), "a+", encoding="utf-8")
    compat.lock(fh)
    fh.write(json.dumps({"pid": 4242}) + "\n")
    fh.flush()
    try:
        # The probe opens the file afresh, and the lock is per open file.
        assert fdsn.holder(state) == {"pid": 4242}
    finally:
        compat.unlock(fh)
        fh.close()
    assert fdsn.holder(state) is None


def test_the_command_plans_adopts_and_fetches(pull, monkeypatch, capsys):
    c, ledger, out = pull
    monkeypatch.setattr(fdsn.requests, "Session",
                        lambda: Service(Answer(404), Answer(200, day_bytes())))
    monkeypatch.setattr("sys.argv", ["tdvms fdsn", "--ledger", str(c.path), "--station",
                                     "KO.SVRC", "--start", "2016-02-29", "--end",
                                     "2016-03-02"])
    assert fdsn.main() == 0
    assert [r["state"] for r in ledger.rows()] == ["nodata", "fetched"]
    assert "done: 1 fetched, 1 nodata" in capsys.readouterr().out
    assert fdsn.holder(fdsn.state_path(c)) is None


def test_the_tui_model_reads_a_pull_and_its_pace(pull):
    c, ledger, out = pull
    assert model.fdsn_pull(c) is None
    fdsn.plan(ledger, "SVRC", [DAY, date(2016, 3, 2)], "KO", "BH?", fdsn.KOERI)
    fdsn.work(ledger, out, session=Service(Answer(200, day_bytes()), Answer(404)), log=str)
    p = model.fdsn_pull(c)
    assert p.stations() == ["SVRC"] and p.counts() == {"fetched": 1, "nodata": 1}
    assert p.live is None and p.rate() == 2
    # A pull that started ten minutes ago did those two days in ten minutes, not an hour.
    p.live = {"started": (datetime.now().astimezone() - timedelta(minutes=10)).isoformat()}
    assert round(p.rate()) == 12


def test_the_fdsn_tab_lists_each_day(pull):
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Static

    from tdvms.tui.app import TdvmsApp
    c, ledger, out = pull
    fdsn.plan(ledger, "SVRC", [DAY, date(2016, 3, 2)], "KO", "BH?", fdsn.KOERI)
    fdsn.work(ledger, out, session=Service(Answer(200, day_bytes()), Answer(404)), log=str)

    async def go():
        app = TdvmsApp(c.path.parent)
        async with app.run_test(size=(180, 40)) as pilot:
            app.query_one("#detail").active = "t-fdsn"
            await pilot.pause(0.3)
            t = app.query_one("#fdsn", DataTable)
            rows = [[str(x) for x in t.get_row_at(i)] for i in range(t.row_count)]
            return rows, str(app.query_one("#fdsn-status", Static).render())
    rows, status = asyncio.run(go())
    assert {(r[0], r[2]) for r in rows} == {("2016-03-01", "fetched"), ("2016-03-02", "nodata")}
    assert "not running" in status and "SVRC" in status


def test_the_cli_lists_fdsn(capsys):
    from tdvms import cli
    cli.usage()
    assert "fdsn" in capsys.readouterr().out
