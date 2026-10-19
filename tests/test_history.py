"""Station history: what earlier campaigns already learnt, asked before planning.

BLKS answered no-data 26 times out of 26. `run` retires such a station within
one campaign, but only after `--nodata-streak` requests -- and the next
campaign to include it starts from nothing and pays them again.
"""
import argparse

from conftest import chunk, write
from tdvms import history, plan
from tdvms.campaign import discover


def _rows(station, *states):
    return [chunk(station, f"2024-{i + 1:02d}-01T00:00:00", state=s, end=f"2024-{i + 1:02d}-22T00:00:00")
            for i, s in enumerate(states)]


def test_a_station_that_never_returned_data_is_flagged_with_its_period(tmp_path):
    write(tmp_path / "van_ledger.jsonl", _rows("BLKS", "nodata", "nodata", "nodata"))
    [rec] = history.gather(discover(tmp_path)).values()
    assert rec.kind == "dead"
    assert "2024-01-01..2024-03-22" in rec.warning() and "(van)" in rec.warning()


def test_one_good_window_means_the_station_works(tmp_path):
    """No-data is about a period. A station with a gap mid-span is not dead."""
    write(tmp_path / "van_ledger.jsonl", _rows("ELBA", "fetched", "nodata", "nodata",
                                               "nodata"))
    assert history.gather(discover(tmp_path))["ELBA"].kind is None


def test_mostly_silent_is_patchy(tmp_path):
    write(tmp_path / "van_ledger.jsonl", _rows("SLVT", "fetched", *["nodata"] * 7))
    rec = history.gather(discover(tmp_path))["SLVT"]
    assert rec.kind == "patchy" and "7 of 8" in rec.warning()


def test_history_is_pooled_across_campaigns(tmp_path):
    write(tmp_path / "van_ledger.jsonl", _rows("MSAY", "nodata", "nodata"))
    write(tmp_path / "elazig_ledger.jsonl", _rows("MSAY", "retired", "nodata"))
    rec = history.gather(discover(tmp_path))["MSAY"]
    assert (rec.nodata, rec.retired, rec.campaigns) == (3, 1, {"van", "elazig"})


def test_pending_windows_are_not_history(tmp_path):
    write(tmp_path / "van_ledger.jsonl", _rows("ELBA", "pending", "submitted"))
    assert history.gather(discover(tmp_path)) == {}


def test_plan_warns_but_still_plans(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "old_ledger.jsonl", _rows("BLKS", *["nodata"] * 8))
    p = plan.add_args(argparse.ArgumentParser())
    p.add_argument("--ledger")
    args = p.parse_args(["--station", "BLKS", "--no-verify", "--start", "2025-01-01",
                         "--end", "2025-02-01", "--ledger", str(tmp_path / "new_ledger.jsonl")])
    plan.run(args)
    out = capsys.readouterr().out
    assert "[!] BLKS: 8 no-data answer(s)" in out and "planned 2 new chunk(s)" in out
