"""Campaigns one after another, and a loop that knows when it is finished.

`run` never exited on its own, so a shell script decided "done" by polling the
ledger every five minutes and stopped the run with SIGINT -- which a job started
with `&` ignores. The finished run lived on beside the next campaign and read
its mail for three hours. `--exit-when-done` and `tdvms queue` replace that.
"""
import json
import pathlib
import zipfile

import pytest

from conftest import FakeMailbox, FakePortal, Rejected, chunk, write
from tdvms import queue, run
from tdvms.ledger import Ledger, now
from tdvms.mailbox import Link
from tdvms.slots import Pool
from tdvms.supervisor import Config

MEMBER = "TU_ELBA_09092025_000000_30092025_000000_HH.mseed"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("TDVMS_NOTIFY", raising=False)


def test_a_ledger_is_complete_when_every_chunk_has_an_outcome(ledger_path):
    assert not Ledger(ledger_path).complete()                 # empty is not done
    led = write(ledger_path, [chunk(state="fetched"),
                              chunk(start="2025-10-01T00:00:00", state="submitted")])
    assert not led.complete()
    led.update("ELBA:2025-10-01T00:00:00", state="nodata")
    assert led.complete()


def _served(monkeypatch):
    from tdvms import fetch
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(MEMBER, b"\0" * 1024)

    class Response:
        headers = {"Content-Type": "application/zip"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, n):
            yield buf.getvalue()
    monkeypatch.setattr(fetch.requests, "get", lambda url, **kw: Response())


def test_exit_when_done_returns_as_the_last_answer_lands(ledger_path, tmp_path, monkeypatch):
    _served(monkeypatch)
    led = write(ledger_path, [chunk(state="submitted", email="you+a1@gmail.com",
                                    submitted_at=now())])
    box = FakeMailbox(Link(b"1", "you+a1@gmail.com", ["https://tdvms.afad.gov.tr/x.zip"]))
    monkeypatch.setattr(run.time, "sleep", lambda s: pytest.fail("slept after done"))
    run.loop(led, Pool("you@gmail.com", 1), Config(out_dir=str(tmp_path / "raw")), box,
             FakePortal(), interval=0, until_done=True)
    assert led.complete()


def test_without_it_the_loop_waits_for_more_work(ledger_path, tmp_path, monkeypatch):
    """New windows can be planned into a running campaign, so `run` alone keeps
    going -- the behaviour every existing invocation relies on."""
    led = write(ledger_path, [chunk(state="fetched")])

    def sleep(s):
        raise StopIteration
    monkeypatch.setattr(run.time, "sleep", sleep)
    with pytest.raises(StopIteration):
        run.loop(led, Pool("you@gmail.com", 1), Config(out_dir=str(tmp_path)), None,
                 None, interval=0)


def _campaign(root, name, *rows):
    home = root / "campaigns" / name
    home.mkdir(parents=True)
    write(home / "ledger.jsonl", list(rows))
    return home / "ledger.jsonl"


def _queue(monkeypatch, root, *specs, portal):
    monkeypatch.setattr(queue, "Mailbox", lambda *a, **k: FakeMailbox())
    monkeypatch.setattr(queue, "Client", lambda **k: portal)
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    p = queue.add_args(__import__("argparse").ArgumentParser())
    return queue.run(p.parse_args([*specs, "--root", str(root),
                                   "--address", "you@gmail.com", "--slots", "2"]))


def test_a_queue_works_its_campaigns_in_order_and_skips_finished_ones(tmp_path, monkeypatch):
    _campaign(tmp_path, "old", chunk("BLKS", state="fetched"))
    _campaign(tmp_path, "first", chunk("AAAA"))
    last = _campaign(tmp_path, "second", chunk("BBBB"))
    # A permanent refusal gives each window an outcome at once, so each
    # campaign completes in one cycle and the queue has to move on.
    portal = FakePortal(*[Rejected("not listed", permanent=True)] * 4)
    assert _queue(monkeypatch, tmp_path, "old", "first", "second", portal=portal) == 0
    assert [c[0] for c in portal.calls] == ["AAAA", "BBBB"]
    lock = tmp_path / "state" / "tdvms" / "you@gmail.com.lock"
    assert str(last.resolve()) in lock.read_text()        # says what it is on


def test_each_queued_campaign_downloads_into_its_own_folder(tmp_path, monkeypatch):
    seen = []
    real = run.loop

    def spy(ledger, pool, cfg, *a, **k):
        seen.append(pathlib.Path(cfg.out_dir))
        ledger.update(next(iter(r for r in ledger.rows()))["station"] + ":"
                      + ledger.rows()[0]["start"], state="nodata")
        return real(ledger, pool, cfg, *a, **k)
    monkeypatch.setattr(run, "loop", spy)
    _campaign(tmp_path, "first", chunk("AAAA"))
    write(tmp_path / "flat_ledger.jsonl", [chunk("BBBB")])
    _queue(monkeypatch, tmp_path, "first", str(tmp_path / "flat_ledger.jsonl"),
           portal=FakePortal())
    assert seen == [tmp_path / "campaigns" / "first" / "raw", tmp_path / "flat_raw"]


def test_an_unknown_campaign_is_refused_before_anything_runs(tmp_path, monkeypatch):
    _campaign(tmp_path, "first", chunk("AAAA"))
    portal = FakePortal()
    with pytest.raises(SystemExit, match="no campaign called 'nope'"):
        _queue(monkeypatch, tmp_path, "first", "nope", portal=portal)
    assert portal.calls == []


def test_run_files_into_the_campaigns_own_folder_by_default(tmp_path, monkeypatch):
    """`--out-dir` used to default to afad_raw for every ledger, so a campaign
    run without it filed its archives beside every other campaign's."""
    from tdvms import campaign
    assert campaign.for_ledger(tmp_path / "silivri_ledger.jsonl").raw == tmp_path / "silivri_raw"
    assert campaign.for_ledger(tmp_path / "tdvms_ledger.jsonl").raw == tmp_path / "afad_raw"
    assert (campaign.for_ledger(tmp_path / "campaigns" / "van" / "ledger.jsonl").raw
            == tmp_path / "campaigns" / "van" / "raw")


def test_the_cli_lists_queue(capsys):
    from tdvms import cli
    cli.usage()
    assert "queue" in capsys.readouterr().out


def test_ledger_rows_round_trip(ledger_path):
    """Guard for the fixtures above: `write` produces what `Ledger` reads."""
    write(ledger_path, [chunk()])
    assert json.loads(ledger_path.read_text())["station"] == "ELBA"
