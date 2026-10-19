"""The Textual app: Campaigns (watch and repair) and Plan (stations off a map).

Drawing only. What a screen shows and every change it makes comes from
`tui.model`, so the app never touches a ledger except through it.
"""
import argparse
import os
import pathlib
from datetime import date
from functools import partial

from rich.text import Text
from textual import on
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, Checkbox, DataTable, Footer, Header, Input,
                             Label, Log, OptionList, Select, Static, TabbedContent,
                             TabPane)
from textual.widgets.option_list import Option

from tdvms import catalog, coverage, credentials, disk, history, notify, settings
from tdvms import plan as planning
from tdvms.ledger import IN_FLIGHT, age_seconds, chunk_id
from tdvms.slots import holder
from tdvms.tui import model
from tdvms.tui.mapview import NETWORK_STYLE, StationMap

NEW = "__new__"
STATE_STYLE = {"pending": "#9aa5b1", "claimed": "bold #ffd43b", "submitted": "#ffd43b",
               "fetched": "#69db7c", "nodata": "#868e96", "failed": "bold #ff6b6b",
               "retired": "#ff8787"}


def fmt_age(seconds):
    if seconds is None:
        return "—"
    if seconds < 60:
        return "<1 min"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    if seconds < 86400 * 2:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"


def plan_defaults():
    """`tdvms plan`'s own defaults, read from its parser so the two agree."""
    p = argparse.ArgumentParser()
    planning.add_args(p)
    return p.get_default("start"), p.get_default("end"), p.get_default("chunk_days")


# --- dialogs ------------------------------------------------------------------

class Confirm(ModalScreen[bool]):
    BINDINGS = [Binding("y", "answer(True)", "Yes"),
                Binding("n,escape", "answer(False)", "No")]

    def __init__(self, title, body, yes="Yes"):
        super().__init__()
        self.title_text, self.body, self.yes = title, body, yes

    def compose(self):
        with Vertical(classes="dialog"):
            yield Label(self.title_text, classes="dialog-title")
            yield Static(self.body, classes="dialog-body")
            with Horizontal(classes="dialog-buttons"):
                yield Button(f"{self.yes} (y)", variant="warning", id="yes")
                yield Button("Cancel (n)", id="no")

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "yes")

    def action_answer(self, value):
        self.dismiss(value)


class RunCommand(ModalScreen[None]):
    """The `tdvms run` line for a freshly planned campaign. Copy, not launch."""
    BINDINGS = [Binding("c", "copy", "Copy"), Binding("escape,enter", "close", "Close")]

    def __init__(self, body, command):
        super().__init__()
        self.body, self.command = body, command

    def compose(self):
        with Vertical(classes="dialog"):
            yield Label("Planned", classes="dialog-title")
            yield Static(self.body, classes="dialog-body")
            yield Static(Text(self.command, style="bold"), classes="command")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Copy (c)", variant="primary", id="copy")
                yield Button("Close", id="close")

    def on_button_pressed(self, event):
        if event.button.id == "copy":
            self.action_copy()
        else:
            self.dismiss()

    def action_copy(self):
        self.app.copy_to_clipboard(self.command)
        self.notify("copied")

    def action_close(self):
        self.dismiss()


class EarthquakePicker(ModalScreen[dict | None]):
    """Pick an event from the shipped catalog, or type an epicentre in."""
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, events):
        super().__init__()
        self.events = sorted(events, key=lambda e: e.time, reverse=True)
        self.chosen = None

    def compose(self):
        with Vertical(classes="dialog wide"):
            yield Label("Earthquake", classes="dialog-title")
            yield Input(placeholder="search place or date · m6 = M6 and up", id="eq-search")
            yield DataTable(id="events", cursor_type="row", zebra_stripes=True)
            with Horizontal(classes="form-row"):
                yield Label("Lat")
                yield Input(id="eq-lat", classes="coord")
                yield Label("Lon")
                yield Input(id="eq-lon", classes="coord")
                yield Label("Date")
                yield Input(id="eq-date", classes="date")
            with Horizontal(classes="form-row"):
                yield Label("Radius km")
                yield Input("50", id="eq-km", classes="num")
                yield Label("Months before")
                yield Input("6", id="eq-before", classes="num")
                yield Label("after")
                yield Input("6", id="eq-after", classes="num")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Pick stations", variant="primary", id="apply")
                yield Button("Cancel", id="cancel")

    def on_mount(self):
        t = self.query_one("#events", DataTable)
        t.add_column("date", key="date")
        t.add_column("M", key="mag")
        t.add_column("depth", key="depth")
        t.add_column("place", key="place")
        self._fill("")
        t.focus()

    @on(Input.Changed, "#eq-search")
    def _search(self, event):
        self._fill(event.value)

    def _fill(self, query):
        t = self.query_one("#events", DataTable)
        t.clear()
        minmag, words = 0.0, []
        for tok in query.lower().split():
            if tok.startswith("m") and tok[1:].replace(".", "", 1).isdigit():
                minmag = float(tok[1:])
            else:
                words.append(tok)
        for e in self.events:
            hay = f"{e.time} {e.place}".lower()
            if e.magnitude < minmag or not all(w in hay for w in words):
                continue
            style = "bold" if e.magnitude >= 6.5 else ""
            t.add_row(e.time[:16].replace("T", " "), Text(f"{e.magnitude:.1f}", style=style),
                      f"{e.depth_km:.0f} km", e.place, key=e.id)

    @on(DataTable.RowHighlighted, "#events")
    def _pick(self, event):
        e = next((e for e in self.events if e.id == event.row_key.value), None)
        if e is None:
            return
        self.chosen = e
        self.query_one("#eq-lat", Input).value = f"{e.latitude:.4f}"
        self.query_one("#eq-lon", Input).value = f"{e.longitude:.4f}"
        self.query_one("#eq-date", Input).value = e.date

    @on(DataTable.RowSelected, "#events")
    def _chosen(self, event):
        self.query_one("#eq-km", Input).focus()

    def on_button_pressed(self, event):
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        try:
            lat = float(self.query_one("#eq-lat", Input).value)
            lon = float(self.query_one("#eq-lon", Input).value)
            when = date.fromisoformat(self.query_one("#eq-date", Input).value.strip())
            km = float(self.query_one("#eq-km", Input).value)
            before = int(self.query_one("#eq-before", Input).value)
            after = int(self.query_one("#eq-after", Input).value)
        except ValueError:
            self.notify("lat, lon, date (YYYY-MM-DD), radius and months must all be "
                        "filled in", severity="error")
            return
        e = self.chosen
        if e and (abs(e.latitude - lat) > 1e-3 or abs(e.longitude - lon) > 1e-3):
            e = None              # edited by hand: no longer that event
        self.dismiss({"lat": lat, "lon": lon, "km": km, "event": e,
                      "start": model.add_months(when, -before).isoformat(),
                      "end": model.add_months(when, after).isoformat()})

    def action_cancel(self):
        self.dismiss(None)


# --- Campaigns ----------------------------------------------------------------

class CampaignsView(Vertical):
    BINDINGS = [
        Binding("r", "reset", "Reset chunk"),
        Binding("i", "file_stray", "File stray"),
        Binding("d", "delete_stray", "Delete duplicate"),
    ]

    def __init__(self, root, **kw):
        super().__init__(**kw)
        self.root = root
        self.campaigns, self.runs = [], []
        self.current = None
        self._sig = {}
        self._log = (None, None)
        self._filing = set()
        self._strays = []
        self._cov_running = False

    def compose(self):
        with Horizontal():
            yield OptionList(id="ledgers")
            with TabbedContent(id="detail"):
                with TabPane("Overview", id="t-overview"):
                    with VerticalScroll():
                        yield Static(id="overview")
                with TabPane("Slots", id="t-slots"):
                    yield DataTable(id="slots", cursor_type="row")
                    yield Static(
                        "Busy cooldowns after a portal 111 live only in the run's memory; "
                        "the Log tab shows them as [BUSY].", classes="hint")
                with TabPane("Chunks", id="t-chunks"):
                    yield Input(placeholder="filter: station, state, +a3, note words …",
                                id="chunk-filter")
                    yield DataTable(id="chunks", cursor_type="row", zebra_stripes=True)
                with TabPane("Log", id="t-log"):
                    yield Log(id="log", max_lines=5000)
                with TabPane("Strays", id="t-strays"):
                    yield Static(
                        "UNMATCHED archives: downloads a run kept because its own ledger "
                        "lacked the window — typically mail read by another run on the same "
                        "address. [b]i[/] files one into the ledger that holds it (copying "
                        "across disks if needed); [b]d[/] deletes one whose window is already "
                        "fetched.", classes="hint")
                    yield DataTable(id="strays", cursor_type="row", zebra_stripes=True)
                with TabPane("Coverage", id="t-coverage"):
                    yield Static(id="cov-status", classes="hint")
                    yield DataTable(id="coverage", cursor_type="row", zebra_stripes=True)

    def on_mount(self):
        self.query_one("#ledgers").border_title = "Ledgers"
        t = self.query_one("#slots", DataTable)
        for name in ("slot", "state", "chunk", "for"):
            t.add_column(name, key=name)
        t = self.query_one("#chunks", DataTable)
        for name in ("state", "chunk", "slot", "since", "tries", "note"):
            t.add_column(name, key=name)
        t = self.query_one("#strays", DataTable)
        for name in ("file", "window", "belongs to", "state there", "size", "action"):
            t.add_column(name, key=name)
        t = self.query_one("#coverage", DataTable)
        for name in ("station", "channel", "covered", "gaps ≥1 min", "largest gap",
                     "requested period →"):
            t.add_column(name, key=name)
        self.tick()
        self.set_interval(2, self.tick)

    def check_action(self, action, parameters):
        tab = self.query_one("#detail", TabbedContent).active
        if action == "reset":
            return tab == "t-chunks"
        if action in ("file_stray", "delete_stray"):
            return tab == "t-strays"
        return True

    @on(TabbedContent.TabActivated, "#detail")
    def _tab(self, event):
        # Focus the tab's table so its keys work, and show in the footer, at once.
        target = {"t-slots": "#slots", "t-chunks": "#chunks", "t-log": "#log",
                  "t-strays": "#strays", "t-coverage": "#coverage"}.get(event.pane.id)
        if target:
            self.query_one(target).focus()
        self.refresh_bindings()
        self._sig.clear()
        self.tick()

    # --- the refresh -------------------------------------------------------

    def campaign(self):
        return next((c for c in self.campaigns if c.name == self.current), None)

    def tick(self):
        self.campaigns = model.discover(self.root)
        self.runs = model.live_runs()
        self._ledger_list()
        c = self.campaign()
        if c is None:
            self.query_one("#overview", Static).update(
                Text(f"No ledgers under {self.root}.\nPlan one on the Plan tab (F2).",
                     style="italic"))
            return
        rows = c.ledger.rows()
        run = model.live_for(c, self.runs)
        tab = self.query_one("#detail", TabbedContent).active
        self._overview(c, run, rows)
        try:
            mtime = c.path.stat().st_mtime_ns
        except OSError:
            mtime = None
        if tab == "t-slots":
            self._slots(c, run, rows, mtime)
        elif tab == "t-chunks":
            self._chunks(c, rows, mtime)
        elif tab == "t-strays":
            self._strays_table()
        elif tab == "t-coverage":
            self._coverage(c, mtime)
        self._tail(c, run)

    def _ledger_list(self):
        ol = self.query_one("#ledgers", OptionList)
        prompts = []
        for c in self.campaigns:
            rows = c.ledger.rows()
            done = model.progress(rows)
            live = model.live_for(c, self.runs)
            queued = None if live else model.queued_for(c, self.runs)
            bar = "█" * round(done * 10) + "░" * (10 - round(done * 10))
            t = Text()
            if live:
                t.append("● ", style="bold #69db7c")
            elif queued:
                t.append("◌ ", style="#4ea8ff")
            else:
                t.append("  ")
            t.append(f"{c.name[:12]:<12} ")
            t.append(bar, style="#69db7c" if done >= 1 else "#4ea8ff")
            t.append(f" {done * 100:3.0f}%")
            prompts.append(t)
        names = [c.name for c in self.campaigns]
        if names != [o.id for o in ol.options]:
            ol.set_options([Option(p, id=n) for p, n in zip(prompts, names)])
            if names:
                if self.current not in names:
                    live = [c.name for c in self.campaigns if model.live_for(c, self.runs)]
                    self.current = (live or names)[0]
                ol.highlighted = names.index(self.current)
        else:
            for i, p in enumerate(prompts):
                ol.replace_option_prompt_at_index(i, p)

    @on(OptionList.OptionHighlighted, "#ledgers")
    def _pick(self, event):
        if event.option.id != self.current:
            self.current = event.option.id
            self._sig.clear()
            self._log = (None, None)
            self.query_one("#log", Log).clear()
            self.tick()

    def _overview(self, c, run, rows):
        t = Text()
        t.append(f"{c.name}", style="bold")
        t.append(f"   {c.path}\n", style="dim")
        queued = None if run else model.queued_for(c, self.runs)
        if run:
            t.append("● live", style="bold #69db7c")
            t.append(f"  pid {run.pid} · {run.address} × {run.slots} slots · "
                     f"out-dir {run.out_dir}\n")
            if run.command == "queue":
                names = [p.parent.name if p.name == "ledger.jsonl"
                         else p.name.removesuffix("_ledger.jsonl") for p in run.queue]
                t.append(f"  queue: {' → '.join(names)}\n", style="dim")
            if run.address:
                who = holder(run.address)
                if who:
                    t.append(f"  pool lock held ({who})\n", style="dim")
                else:
                    t.append("  pool lock not held — this run started before the lock "
                             "existed, so a second run on its address would not be "
                             "refused. Restart it when convenient.\n", style="#ffd43b")
        elif queued:
            q, ahead = queued
            t.append("◌ queued", style="bold #4ea8ff")
            t.append(f"  in pid {q.pid}'s queue, {ahead} campaign(s) from now\n")
        else:
            try:
                age = fmt_age(age_seconds(_iso_mtime(c.path)))
            except OSError:
                age = "—"
            t.append("○ not running", style="dim")
            t.append(f"  · ledger last written {age} ago\n", style="dim")
        free = disk.free_bytes(run.out_dir if run and run.out_dir else c.raw)
        need = disk.reserve_bytes(rows)
        t.append(f"  disk: {free / 1e9:,.0f} GB free for downloads; the loop pauses "
                 f"below {need / 1e9:.1f} GB\n",
                 style="bold #ff8787" if free < need else "dim")
        t.append("\n")
        for line in model.overview(c):
            style = None
            if "STUCK" in line or "FAILED" in line or "never returned data" in line:
                style = "#ff8787"
            t.append(line + "\n", style=style)
        self.query_one("#overview", Static).update(t)

    def _slots(self, c, run, rows, mtime):
        sig = ("slots", c.name, mtime, run and run.pid)
        if self._sig.get("slots") == sig:
            return
        self._sig["slots"] = sig
        t = self.query_one("#slots", DataTable)
        t.clear()
        pool = model.pool_for(c, run, rows)
        if pool is None:
            return
        pool.refresh(c.ledger)
        for slot in pool.slots.values():
            short = "+" + slot.email.split("+", 1)[-1].split("@")[0]
            r = slot.holding
            if r:
                stamp = r.get("submitted_at") or r.get("claimed_at")
                t.add_row(short, Text(r["state"], style=STATE_STYLE[r["state"]]),
                          chunk_id(r["station"], r["start"][:10]),
                          fmt_age(age_seconds(stamp)), key=slot.email)
            else:
                t.add_row(short, Text("free", style="dim"), "", "", key=slot.email)

    @on(Input.Changed, "#chunk-filter")
    def _filter(self, event):
        self._sig.pop("chunks", None)
        self.tick()

    def _chunks(self, c, rows, mtime):
        query = self.query_one("#chunk-filter", Input).value.lower().split()
        sig = ("chunks", c.name, mtime, tuple(query))
        if self._sig.get("chunks") == sig:
            return
        self._sig["chunks"] = sig
        t = self.query_one("#chunks", DataTable)
        keep = t.cursor_row
        t.clear()
        for r in sorted(rows, key=lambda r: (r["station"], r["start"])):
            cid = chunk_id(r["station"], r["start"])
            slot = ("+" + r["email"].split("+", 1)[-1].split("@")[0]) if r.get("email") else ""
            hay = f"{r['state']} {cid} {slot} {r.get('note') or ''}".lower()
            if not all(q in hay for q in query):
                continue
            stamp = {"claimed": "claimed_at", "submitted": "submitted_at"}.get(
                r["state"], "fetched_at")
            since = fmt_age(age_seconds(r.get(stamp))) if r["state"] != "pending" else ""
            t.add_row(Text(r["state"], style=STATE_STYLE.get(r["state"], "")),
                      f"{r['station']}:{r['start'][:10]}", slot, since,
                      str(r.get("attempts") or 0), (r.get("note") or "")[:80], key=cid)
        if t.row_count:
            t.move_cursor(row=min(keep, t.row_count - 1))

    def _strays_table(self):
        self._strays = model.strays(self.campaigns)
        sig = ("strays", tuple((str(s.path), s.status) for s in self._strays),
               tuple(self._filing))
        if self._sig.get("strays") == sig:
            return
        self._sig["strays"] = sig
        t = self.query_one("#strays", DataTable)
        t.clear()
        for s in self._strays:
            window = f"{s.station}:{s.start:%Y-%m-%d}" if s.start else "?"
            action = {"file": "i  file", "duplicate": "d  delete",
                      "orphan": "no ledger holds this window"}[s.status]
            if str(s.path) in self._filing:
                action = "filing…"
            try:
                size = f"{s.path.stat().st_size / 1e6:,.0f} MB"
            except OSError:
                size = "?"
            t.add_row(s.path.name, window, s.campaign.name if s.campaign else "—",
                      Text(s.row["state"], style=STATE_STYLE.get(s.row["state"], ""))
                      if s.row else "—", size, action, key=str(s.path))

    def _tail(self, c, run):
        path = run.log if run and run.log else (c.log if c.log.exists() else None)
        log = self.query_one("#log", Log)
        if path != self._log[0]:
            log.clear()
            self._log = (path, None)
            if path is None:
                log.write_line(f"(no log: a run started by hand would usually write to "
                               f"{c.log.name})")
                return
            log.write_line(f"── {path}")
        if path is None:
            return
        text, offset = model.tail(path, self._log[1])
        self._log = (path, offset)
        if text:
            log.write(text)

    # --- coverage ----------------------------------------------------------

    COV_STYLE = {"full": "#69db7c", "partial": "#ffd43b", "gap": "#ff8787",
                 "nodata": "#a5525a", "pending": "#5c6370", "none": ""}

    def _coverage(self, c, mtime):
        sig = ("coverage", c.name, mtime)
        if self._sig.get("coverage") == sig or self._cov_running:
            return
        self._sig["coverage"] = sig
        self._cov_running = True
        self.query_one("#cov-status", Static).update(
            Text(f"reading the archives under {c.raw} …", style="dim"))
        self.run_worker(partial(self._cov_work, c), thread=True)

    def _cov_work(self, c):
        def progress(done, total, name):
            if done < total:
                self.app.call_from_thread(
                    self.query_one("#cov-status", Static).update,
                    Text(f"reading {done + 1} of {total} new archive(s): {name}", style="dim"))
        try:
            stations, err = coverage.report(c.ledger, c.raw, progress=progress), None
        except Exception as e:
            stations, err = [], f"{type(e).__name__}: {e}"
        self.app.call_from_thread(self._cov_show, c, stations, err)

    def _cov_show(self, c, stations, err):
        self._cov_running = False
        if c.name != self.current:
            self._sig.pop("coverage", None)
            return
        status = self.query_one("#cov-status", Static)
        if err:
            status.update(Text(f"coverage failed: {err}", style="#ff8787"))
            return
        legend = Text()
        legend.append(f"{len(stations)} station(s).  ")
        for ch, kind, what in (("█", "full", "data"), ("▄", "partial", "partial"),
                               ("▁", "gap", "gap in an archive"),
                               ("×", "nodata", "no data at source"),
                               ("·", "pending", "not downloaded yet")):
            legend.append(ch, style=self.COV_STYLE[kind])
            legend.append(f" {what}  ")
        status.update(legend)
        t = self.query_one("#coverage", DataTable)
        t.clear()
        width = 48
        for st in stations:
            rows = st.channels or [None]
            for i, ch in enumerate(rows):
                line = Text()
                for char, kind in coverage.timeline(st, ch, width):
                    line.append(char, style=self.COV_STYLE[kind])
                if ch is None:
                    t.add_row(st.code, "—", "", "", "no archives yet", line,
                              key=f"{st.code}")
                    continue
                gaps = [g for g in ch.gaps if g.seconds >= 60]
                big = max(gaps, key=lambda g: g.seconds, default=None)
                pct = ch.fraction * 100
                t.add_row(st.code if i == 0 else "", ch.label,
                          Text(f"{pct:5.1f}%", style="#69db7c" if pct >= 99 else
                               "#ffd43b" if pct >= 75 else "#ff8787"),
                          str(len(gaps)),
                          f"{coverage.fmt_seconds(big.seconds)} ({big.why})" if big else "",
                          line, key=ch.cid)

    # --- repairs -----------------------------------------------------------

    def action_reset(self):
        c, t = self.campaign(), self.query_one("#chunks", DataTable)
        if c is None or not t.row_count:
            return
        cid = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        row = c.ledger.by_id(cid)
        if row is None:
            return
        if row["state"] == "pending":
            self.notify(f"{cid} is already pending")
            return
        body = f"{cid} is [b]{row['state']}[/]. It goes back to pending, its stamps cleared."
        if row["state"] in IN_FLIGHT:
            body += ("\n\nIts request is still in the portal's queue. This frees the slot "
                     "here, not at the portal, so that slot's next submission may come "
                     "back BUSY and cool down.")
        if row["state"] == "fetched":
            body += "\n\nThe archive on disk stays; a new fetch is kept beside it as .dup."
        if model.live_for(c, self.runs):
            body += "\n\nA run is live on this ledger; it sees the change next cycle."

        def done(yes):
            if yes:
                model.reset(c, cid)
                self.notify(f"{cid}: {row['state']} → pending")
                self._sig.clear()
                self.tick()
        self.app.push_screen(Confirm("Reset chunk", body, yes="Reset"), done)

    def _stray(self):
        t = self.query_one("#strays", DataTable)
        if not t.row_count:
            return None
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        return next((s for s in self._strays if str(s.path) == key), None)

    def action_file_stray(self):
        s = self._stray()
        if s is None or str(s.path) in self._filing:
            return
        if s.status != "file":
            self.notify(f"{s.path.name}: {s.status}, nothing to file", severity="warning")
            return
        body = (f"{s.path.name}\n→ {s.campaign.name}: "
                f"{chunk_id(s.row['station'], s.row['start'][:10])} "
                f"(now {s.row['state']}), filed under {s.campaign.raw}")

        def done(yes):
            if yes:
                self._filing.add(str(s.path))
                self._sig.pop("strays", None)
                self.run_worker(partial(self._file, s), thread=True)
        self.app.push_screen(Confirm("File stray", body, yes="File"), done)

    def _file(self, s):
        try:
            msg, sev = model.file_stray(s), "information"
        except Exception as e:
            msg, sev = f"{s.path.name}: {type(e).__name__}: {e}", "error"
        self.app.call_from_thread(self._filed, s, msg, sev)

    def _filed(self, s, msg, sev):
        self._filing.discard(str(s.path))
        self.notify(msg, severity=sev, timeout=8)
        self._sig.clear()
        self.tick()

    def action_delete_stray(self):
        s = self._stray()
        if s is None:
            return
        if s.status != "duplicate":
            self.notify(f"{s.path.name} is not a duplicate; only those can be deleted "
                        f"here", severity="warning")
            return
        body = (f"{s.path.name}\n{s.campaign.name} already has this window fetched "
                f"({s.row.get('bytes') or 0:,} bytes on record). Delete the stray copy?")

        def done(yes):
            if yes:
                model.delete_stray(s)
                self.notify(f"deleted {s.path.name}")
                self._sig.clear()
                self.tick()
        self.app.push_screen(Confirm("Delete duplicate", body, yes="Delete"), done)


def _iso_mtime(path):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


# --- Plan ---------------------------------------------------------------------

class PlanView(Vertical):
    BINDINGS = [
        Binding("e", "earthquake", "Earthquake"),
        Binding("x", "clear_epicentre", "Clear epicentre"),
        Binding("c", "clear_picks", "Clear picks"),
        Binding("space", "toggle_row", "Pick", show=False),
    ]

    def __init__(self, root, **kw):
        super().__init__(**kw)
        self.root = root
        self.stations = catalog.load_stations()
        self.events = None
        self.picked = {}          # Station.key -> Station
        self.ref = None           # (lat, lon) the km column measures from
        self.campaigns = []
        self.history = {}         # bare code -> history.Record, from every ledger

    def compose(self):
        start, end, chunk = plan_defaults()
        with Horizontal(id="plan-top"):
            with Vertical(id="map-col"):
                yield StationMap(id="map")
                yield Static(id="map-status")
            with Vertical(id="side"):
                yield Input(placeholder="search station code", id="st-search")
                with Horizontal(id="side-opts"):
                    yield Checkbox("TK strong-motion", id="tk")
                    yield Checkbox("picked only", id="picked-only")
                yield DataTable(id="stations", cursor_type="row", zebra_stripes=True)
        with Horizontal(classes="form-row"):
            yield Label("Start")
            yield Input(start, id="start", classes="date")
            yield Label("End")
            yield Input(end, id="end", classes="date")
            yield Label("Window")
            yield Input(str(chunk), id="chunk", classes="num")
            yield Label("days")
            yield Button("Earthquake… (e)", id="eq")
        with Horizontal(classes="form-row"):
            yield Label("Into")
            yield Select([("+ new campaign", NEW)], id="target", allow_blank=False,
                         value=NEW)
            yield Input(placeholder="new campaign name", id="new-name")
            yield Input(placeholder="downloads: campaigns/<name>/raw, or a folder on "
                                    "another disk", id="raw-dir")
            yield Button("Plan", variant="primary", id="go")
        yield Static(id="preview")

    def on_mount(self):
        t = self.query_one("#stations", DataTable)
        for label, key in (("", "pick"), ("code", "code"), ("net", "net"),
                           ("lat", "lat"), ("lon", "lon"), ("dev", "dev"), ("km", "km"),
                           ("past", "past")):
            t.add_column(label, key=key)
        self.query_one("#map").border_title = (
            " ".join(f"[{NETWORK_STYLE[n]}]{'·' if n in catalog.STRONG_MOTION else '●'}"
                     f"[/] {n}" for n in catalog.NETWORKS)
            + "   ◉ picked  ◆ in ledger  ⊘ never returned data  [#ff6b6b]✱[/] epicentre")
        self.refresh_targets()
        self._restation()
        self._preview()

    # --- what is shown -----------------------------------------------------

    def visible(self):
        tk = self.query_one("#tk", Checkbox).value
        return [s for s in self.stations if tk or s.network not in catalog.STRONG_MOTION]

    def _restation(self):
        m = self.query_one("#map", StationMap)
        m.stations = self.visible()
        m.selected = set(self.picked)
        m._hover()
        m.refresh()
        self._table()

    def _table(self):
        t = self.query_one("#stations", DataTable)
        q = self.query_one("#st-search", Input).value.strip().upper()
        only = self.query_one("#picked-only", Checkbox).value
        rows = [s for s in self.visible()
                if (not q or q in s.code) and (not only or s.key in self.picked)]
        if self.ref:
            lat, lon = self.ref
            dist = {s.key: catalog.distance_km(lat, lon, s.latitude, s.longitude)
                    for s in rows}
            rows.sort(key=lambda s: dist[s.key])
        else:
            dist = {}
            rows.sort(key=lambda s: (s.code, s.network))
        t.clear()
        for s in rows:
            t.add_row("◉" if s.key in self.picked else "", s.code,
                      Text(s.network, style=NETWORK_STYLE.get(s.network, "")),
                      f"{s.latitude:.3f}", f"{s.longitude:.3f}", s.devices or "—",
                      f"{dist[s.key]:.0f}" if s.key in dist else "", self._past(s.code),
                      key=s.key)
        self.query_one("#side").border_title = (
            f"Stations — {len(rows)} shown, {len(self.picked)} picked")

    def _past(self, code):
        """`12✓ 3✗`: windows fetched and answered no-data across every ledger."""
        rec = self.history.get(code)
        if rec is None or not (rec.answered or rec.retired):
            return ""
        style = {"dead": "#ff8787", "patchy": "#ffd43b"}.get(rec.kind, "dim")
        return Text(f"{rec.fetched}✓ {rec.nodata}✗", style=style)

    def refresh_targets(self):
        self.campaigns = model.discover(self.root)
        self.history = history.gather(self.campaigns)
        m = self.query_one("#map", StationMap)
        m.flags = {code: rec.kind for code, rec in self.history.items() if rec.kind}
        sel = self.query_one("#target", Select)
        old = sel.value
        opts = [(c.name, c.name) for c in self.campaigns] + [("+ new campaign", NEW)]
        sel.set_options(opts)
        sel.value = old if old in [v for _, v in opts] else NEW
        self._target_changed()

    def target(self):
        v = self.query_one("#target", Select).value
        return next((c for c in self.campaigns if c.name == v), None)

    @on(Select.Changed, "#target")
    def _target_changed(self, event=None):
        new = self.query_one("#target", Select).value == NEW
        self.query_one("#new-name").display = new
        self.query_one("#raw-dir").display = new
        c = self.target()
        m = self.query_one("#map", StationMap)
        m.planned = {r["station"] for r in c.ledger.rows()} if c else set()
        m.refresh()
        self._preview()

    @on(Input.Changed, "#st-search")
    @on(Checkbox.Changed, "#picked-only")
    def _filter(self, event):
        self._table()

    @on(Checkbox.Changed, "#tk")
    def _tk(self, event):
        if not event.value:
            dropped = [k for k, s in self.picked.items()
                       if s.network in catalog.STRONG_MOTION]
            for k in dropped:
                del self.picked[k]
            if dropped:
                self.notify(f"{len(dropped)} TK station(s) unpicked")
        self._restation()
        self._preview()

    # --- picking -----------------------------------------------------------

    def toggle(self, s):
        if s.key in self.picked:
            del self.picked[s.key]
        elif not s.requestable:
            self.notify(f"{s.code} lists no H, L or N instrument; the portal would "
                        f"refuse it", severity="warning")
            return
        else:
            self.picked[s.key] = s
        m = self.query_one("#map", StationMap)
        m.selected = set(self.picked)
        m.refresh()
        t = self.query_one("#stations", DataTable)
        try:
            t.update_cell(s.key, "pick", "◉" if s.key in self.picked else "")
        except Exception:
            pass
        self.query_one("#side").border_title = (
            f"Stations — {t.row_count} shown, {len(self.picked)} picked")
        self._preview()

    def on_station_map_toggled(self, message):
        self.toggle(message.station)

    def on_station_map_hovered(self, message):
        s = message.station
        t = Text(f"{message.lat:.3f}°N {message.lon:.3f}°E", style="dim")
        if s is not None:
            t.append("   ")
            t.append(f"{s.code}", style=f"bold {NETWORK_STYLE.get(s.network, '')}")
            t.append(f" {s.network}  {s.latitude:.3f}, {s.longitude:.3f}  "
                     f"instruments {s.devices or '—'}")
            if self.ref:
                t.append(f"  · {catalog.distance_km(*self.ref, s.latitude, s.longitude):.0f}"
                         f" km from the epicentre")
            if s.key in self.picked:
                t.append("  · picked", style="bold")
            table = self.query_one("#stations", DataTable)
            if self.query_one("#map").has_focus:
                try:
                    table.move_cursor(row=table.get_row_index(s.key))
                except Exception:
                    pass
        self.query_one("#map-status", Static).update(t)

    @on(DataTable.RowHighlighted, "#stations")
    def _row(self, event):
        table = self.query_one("#stations", DataTable)
        if not table.has_focus:
            return
        s = next((s for s in self.stations if s.key == event.row_key.value), None)
        if s:
            self.query_one("#map", StationMap).go(s.longitude, s.latitude)

    @on(DataTable.RowSelected, "#stations")
    def _select(self, event):
        s = next((s for s in self.stations if s.key == event.row_key.value), None)
        if s:
            self.toggle(s)

    def action_toggle_row(self):
        t = self.query_one("#stations", DataTable)
        if t.has_focus and t.row_count:
            key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
            s = next((s for s in self.stations if s.key == key), None)
            if s:
                self.toggle(s)

    def action_clear_picks(self):
        self.picked.clear()
        self._restation()
        self._preview()

    # --- earthquake mode ---------------------------------------------------

    def action_earthquake(self):
        if self.events is None:
            self.events = catalog.load_events()
        self.app.push_screen(EarthquakePicker(self.events), self._quake)

    def _quake(self, got):
        if not got:
            return
        lat, lon, km = got["lat"], got["lon"], got["km"]
        self.ref = (lat, lon)
        near = [s for s in self.visible() if s.requestable
                and catalog.distance_km(lat, lon, s.latitude, s.longitude) <= km]
        self.picked = {s.key: s for s in near}
        self.query_one("#start", Input).value = got["start"]
        self.query_one("#end", Input).value = got["end"]
        e = got["event"]
        name = self.query_one("#new-name", Input)
        if e is not None and self.query_one("#target", Select).value == NEW and not name.value:
            name.value = model.suggest_name(e)
        m = self.query_one("#map", StationMap)
        m.epicentre = (lat, lon, km)
        m.selected = set(self.picked)
        m.frame_radius(lat, lon, km)
        self._table()
        self._preview()
        what = f"M{e.magnitude:.1f} {e.place}" if e else f"{lat:.3f}, {lon:.3f}"
        self.notify(f"{len(near)} station(s) within {km:g} km of {what}")

    def action_clear_epicentre(self):
        self.ref = None
        m = self.query_one("#map", StationMap)
        m.epicentre = None
        m.refresh()
        self._table()

    def on_button_pressed(self, event):
        if event.button.id == "eq":
            self.action_earthquake()
        elif event.button.id == "go":
            self.action_plan()

    # --- preview and plan --------------------------------------------------

    @on(Input.Changed, "#start, #end, #chunk, #new-name")
    def _inputs(self, event):
        self._preview()

    def _form(self):
        """`(codes, start, end, chunk, problems)` from the form as it stands."""
        problems = []
        try:
            start = date.fromisoformat(self.query_one("#start", Input).value.strip())
            end = date.fromisoformat(self.query_one("#end", Input).value.strip())
            if end <= start:
                problems.append("the end date must be after the start")
        except ValueError:
            start = end = None
            problems.append("dates are YYYY-MM-DD")
        try:
            chunk = int(self.query_one("#chunk", Input).value)
            if chunk < 1:
                raise ValueError
        except ValueError:
            chunk = None
            problems.append("the window is a whole number of days")
        if self.query_one("#target", Select).value == NEW:
            name = self.query_one("#new-name", Input).value.strip()
            if not model.NAME_RE.match(name):
                problems.append("name the new campaign: lowercase letters, digits, - and _")
            elif any(c.name == name for c in self.campaigns):
                problems.append(f"{name!r} already exists — pick it under Into instead")
        if not self.picked:
            problems.append("pick at least one station")
        codes = sorted({s.code for s in self.picked.values()})
        return codes, start, end, chunk, problems

    def _preview(self):
        codes, start, end, chunk, problems = self._form()
        t = Text()
        c = self.target()
        if start and end and chunk and codes:
            rows = c.ledger.rows() if c else []
            total, new = model.preview(rows, codes, start.isoformat(), end.isoformat(),
                                       chunk)
            n = len(model.windows(start.isoformat(), end.isoformat(), chunk))
            t.append(f"{len(codes)} station(s) × {n} window(s) of {chunk} d = {total} chunk(s)",
                     style="bold")
            t.append(f" — {new} new")
            if total - new:
                t.append(f", {total - new} already in {c.name}")
            t.append("\n")
        for warning in self._warnings():
            t.append(f"! {warning}\n", style="#ffd43b")
        for p in problems:
            t.append(f"· {p}\n", style="#ff8787")
        self.query_one("#preview", Static).update(t)
        self.query_one("#go", Button).disabled = bool(problems)

    def _warnings(self):
        out = []
        for code in sorted({s.code for s in self.picked.values()}):
            rec = self.history.get(code)
            if rec and rec.warning():
                out.append(rec.warning())
        tk = sorted(s.code for s in self.picked.values()
                    if s.network in catalog.STRONG_MOTION)
        if tk and len(tk) < len(self.picked):
            out.append(f"{', '.join(tk[:6])}{' …' if len(tk) > 6 else ''}: strong-motion "
                       f"accelerometers, picked alongside broadband stations — a "
                       f"different instrument class")
        by_code = {}
        for s in self.stations:
            by_code.setdefault(s.code, []).append(s.network)
        for s in self.picked.values():
            nets = by_code.get(s.code, [])
            if len(nets) > 1 and s.network != "TU" and "TU" in nets:
                out.append(f"{s.code} is listed in {' and '.join(nets)}; ledgers key by "
                           f"code and `run` requests the TU one")
        return out

    def action_plan(self):
        codes, start, end, chunk, problems = self._form()
        if problems:
            self.notify(problems[0], severity="error")
            return
        start, end = start.isoformat(), end.isoformat()
        c = self.target()
        if c is None:
            name = self.query_one("#new-name", Input).value.strip()
            raw = self.query_one("#raw-dir", Input).value.strip()
            into = (f"new campaign [b]{name}[/] — campaigns/{name}/ledger.jsonl, "
                    f"downloads in {raw or f'campaigns/{name}/raw'}")
        else:
            name, raw = c.name, None
            into = f"[b]{c.name}[/] ({c.path.name})"
        body = (f"{len(codes)} station(s): {', '.join(codes)}\n"
                f"{start} → {end}, {chunk}-day windows\ninto {into}")

        def done(yes):
            if not yes:
                return
            try:
                target = c or model.new_campaign(self.root, name, raw or None)
                added = model.apply_plan(target, codes, start, end, chunk)
            except (ValueError, OSError) as e:
                self.notify(str(e), severity="error")
                return
            self.refresh_targets()
            self.query_one("#target", Select).value = target.name
            self.query_one("#new-name", Input).value = ""
            self.query_one("#raw-dir", Input).value = ""
            address, slots = _address_hint(self.root)
            rows = target.ledger.rows()
            msg = (f"{added} new chunk(s) for {len(codes)} station(s) in {target.name}; "
                   f"the ledger holds {len(rows)}, "
                   f"{sum(1 for r in rows if r['state'] == 'pending')} pending.\n\n"
                   f"Start the loop yourself — the TUI only watches:")
            unset = [v for v, (val, _) in credentials.resolve().items() if not val]
            if unset:
                msg += (f"\n\n[#ffd43b]The mailbox is not set up ({', '.join(unset)}); "
                        f"fill it in on the Mailbox tab (F3) first.[/]")
            self.app.push_screen(RunCommand(msg, model.run_command(
                target, self.root, address, slots)))
        self.app.push_screen(Confirm("Plan", body, yes="Plan"), done)


def _address_hint(root):
    """The address and pool size to suggest.

    What Settings says first -- it is the operator's own choice -- then a live
    run's, then what an existing ledger used.
    """
    address = settings.address()
    try:
        slots = settings.slots() if settings.get(settings.SLOTS)[0] else None
    except ValueError:
        slots = None
    if not (address and slots):
        runs = [r for r in model.live_runs() if r.address and not r.dry_run]
        guesses = [(r.address, r.slots) for r in runs] + [
            g for c in model.discover(root) if (g := model.infer_pool(c.ledger.rows()))]
        if guesses:
            address, slots = address or guesses[0][0], slots or guesses[0][1]
    return address, slots or settings.DEFAULT_SLOTS


def _home(p):
    """A path with the home directory written as `~`."""
    home = str(pathlib.Path.home())
    return "~" + str(p)[len(home):] if str(p).startswith(home + os.sep) else str(p)


# --- Mailbox ------------------------------------------------------------------

class MailboxView(VerticalScroll):
    """The three IMAP variables: shown with where each comes from, tested, saved."""

    FIELDS = (
        ("TDVMS_IMAP_HOST", "IMAP server", "imap.gmail.com", False),
        ("TDVMS_IMAP_USER", "Email address", "you@gmail.com", False),
        ("TDVMS_IMAP_PASS", "Password", "an app password, not your account password", True),
    )

    def compose(self):
        yield Static(
            "tdvms reads the portal's replies from an inbox over IMAP, so it needs the "
            "server, the address and a password.\n\n"
            "[b]Gmail:[/] turn on 2-step verification, create an app password at "
            "[u]https://myaccount.google.com/apppasswords[/u], and use [b]imap.gmail.com[/]. "
            "The address must accept plus-addressing (you+a1@gmail.com reaching "
            "you@gmail.com), which Gmail does — each +aN is one download slot.\n\n"
            f"Saved to [b]{_home(credentials.path())}[/], readable only by you. A variable of the "
            "same name in the environment always takes precedence over the file.",
            classes="intro")
        for var, label, hint, secret in self.FIELDS:
            with Horizontal(classes="cred-row"):
                yield Label(label, classes="cred-label")
                yield Input(placeholder=hint, password=secret, id=f"v-{var}")
            yield Static(id=f"s-{var}", classes="cred-source")
        yield Checkbox("show password", id="show-pass")
        with Horizontal(classes="cred-buttons"):
            yield Button("Test connection", id="test")
            yield Button("Save", variant="primary", id="save")
        yield Static(id="cred-result")
        yield Static(
            "[b]Download slots[/]\n\n"
            "How many requests `run` and `queue` keep at the portal at once. The portal "
            "handles one request per email address, so each slot is its own "
            "plus-address — you+a1@…, you+a2@… — all delivering to the mailbox above. "
            "More slots, more data in flight; 8 is a comfortable start.\n\n"
            f"Used whenever --slots is not given. Saved to [b]{_home(settings.path())}[/]; "
            "TDVMS_SLOTS in the environment takes precedence.", classes="intro section")
        with Horizontal(classes="cred-row"):
            yield Label("Slots", classes="cred-label")
            yield Input(placeholder=str(settings.DEFAULT_SLOTS), id="slots",
                        type="integer", max_length=3)
        yield Static(id="slots-source", classes="cred-source")
        with Horizontal(classes="cred-buttons"):
            yield Button("Save", variant="primary", id="slots-save")
        yield Static(
            "[b]Notifications[/]\n\n"
            "Where `run` and `queue` say so when a campaign finishes or needs you: the "
            "mailbox failing, the disk nearly full, every slot stuck on BUSY, nothing "
            "back for hours. One or more, comma-separated: an ntfy topic URL — pick an "
            "unguessable topic at [u]https://ntfy.sh[/u] and subscribe in its phone app — "
            "any URL that accepts a POST, or [b]desktop[/].\n\n"
            f"Saved to [b]{_home(notify.path())}[/]. TDVMS_NOTIFY in the environment, "
            "or --notify on the command line, take precedence.", classes="intro section")
        with Horizontal(classes="cred-row"):
            yield Label("Send to", classes="cred-label")
            yield Input(placeholder="https://ntfy.sh/your-topic, desktop", id="notify")
        yield Static(id="notify-source", classes="cred-source")
        with Horizontal(classes="cred-buttons"):
            yield Button("Send a test", id="notify-test")
            yield Button("Save", variant="primary", id="notify-save")
        yield Static(id="notify-result")

    def on_mount(self):
        for var, (value, _) in credentials.resolve().items():
            self.query_one(f"#v-{var}", Input).value = value or ""
        self.query_one("#notify", Input).value = ", ".join(notify.targets())
        self.query_one("#slots", Input).value = settings.get(settings.SLOTS)[0] or ""
        self._sources()

    def _values(self):
        return {var: self.query_one(f"#v-{var}", Input).value.strip()
                for var, *_ in self.FIELDS}

    def _notify_source(self):
        t = Text("TDVMS_NOTIFY: ", style="dim")
        if os.environ.get(notify.ENV):
            t.append("from the environment, which overrides what is saved here",
                     style="#ffd43b")
        elif notify.targets():
            t.append(f"saved in {_home(notify.path())}", style="#69db7c")
        else:
            t.append("not set — no notifications", style="dim")
        self.query_one("#notify-source", Static).update(t)

    def _typed_slots(self):
        """The slot count typed, or None when the box is empty (meaning 8)."""
        text = self.query_one("#slots", Input).value.strip()
        if not text:
            return None
        n = int(text) if text.isdigit() else 0
        if not 1 <= n <= 99:
            raise ValueError("slots is a whole number from 1 to 99")
        return n

    def _slots_source(self):
        t = Text("TDVMS_SLOTS: ", style="dim")
        value, source = settings.get(settings.SLOTS)
        if source and source.startswith("environment"):
            t.append(f"{value}, from the environment, which overrides what is saved here",
                     style="#ffd43b")
        elif source:
            t.append(f"{value}, saved in {_home(source)}", style="#69db7c")
        else:
            t.append(f"not set — {settings.DEFAULT_SLOTS} by default", style="dim")
        base = self.query_one("#v-TDVMS_IMAP_USER", Input).value.strip()
        try:
            n = self._typed_slots() or settings.DEFAULT_SLOTS
            if "@" in base:
                from tdvms.slots import pool
                first, *rest = pool(base, n)
                t.append(f"\n{first}" + (f" … {rest[-1]}" if rest else ""), style="dim")
        except ValueError as e:
            t.append(f"\n{e}", style="#ff8787")
        self.query_one("#slots-source", Static).update(t)

    def _save_slots(self):
        try:
            n = self._typed_slots()
        except ValueError as e:
            self.notify(str(e), severity="error")
            return
        where = settings.save({settings.SLOTS: str(n) if n else ""})
        self._slots_source()
        self.notify(f"{n} slot(s) saved to {where}" if n
                    else f"slots back to the default of {settings.DEFAULT_SLOTS}")

    def _sources(self):
        self._notify_source()
        self._slots_source()
        typed = self._values()
        for var, (value, source) in credentials.resolve().items():
            t = Text(f"{var}: ", style="dim")
            if source is None:
                t.append("not set", style="#ff8787")
            elif source.startswith("environment"):
                t.append(f"from the {source}", style="#ffd43b")
                if typed[var] != value:
                    t.append(" — overrides what is typed here; `run` will use the "
                             "environment's value", style="#ffd43b")
            else:
                t.append(f"saved in {source}", style="#69db7c")
            self.query_one(f"#s-{var}", Static).update(t)

    @on(Checkbox.Changed, "#show-pass")
    def _show(self, event):
        self.query_one("#v-TDVMS_IMAP_PASS", Input).password = not event.value

    @on(Input.Changed)
    def _typed(self, event):
        self._sources()

    def on_button_pressed(self, event):
        if event.button.id in ("notify-test", "notify-save"):
            self._notify_button(event)
            return
        if event.button.id == "slots-save":
            self._save_slots()
            return
        values = self._values()
        missing = [label for var, label, *_ in self.FIELDS if not values[var]]
        if missing:
            self.notify(f"fill in: {', '.join(missing)}", severity="error")
            return
        result = self.query_one("#cred-result", Static)
        if event.button.id == "save":
            where = credentials.write_file(values)
            self._sources()
            self.notify(f"saved to {where}")
        elif event.button.id == "test":
            event.button.disabled = True
            result.update(Text(f"connecting to {values['TDVMS_IMAP_HOST']} …", style="dim"))
            self.run_worker(partial(self._test, values), thread=True)

    def _notify_button(self, event):
        value = self.query_one("#notify", Input).value.strip()
        result = self.query_one("#notify-result", Static)
        if event.button.id == "notify-save":
            where = notify.save(value)
            self._notify_source()
            self.notify(f"saved to {where}" if value else "notifications turned off")
            return
        targets = notify.split(value)
        if not targets:
            self.notify("enter at least one target", severity="error")
            return
        event.button.disabled = True
        result.update(Text("sending …", style="dim"))
        self.run_worker(partial(self._notify_test, targets), thread=True)

    def _notify_test(self, targets):
        failed = notify.Notifier(targets, log=lambda m: None).send(
            "tdvms: test notification", "If you can read this, notifications work.")
        ok = [t for t in targets if t not in {f for f, _ in failed}]
        msg = Text()
        if ok:
            msg.append(f"✓ sent to {', '.join(ok)}\n", style="#69db7c")
        for t, err in failed:
            msg.append(f"✗ {t}: {err}\n", style="#ff8787")
        self.app.call_from_thread(self._notify_tested, msg)

    def _notify_tested(self, msg):
        self.query_one("#notify-test", Button).disabled = False
        self.query_one("#notify-result", Static).update(msg)

    def _test(self, values):
        try:
            msg, style = credentials.check(*(values[v] for v in credentials.VARS)), "#69db7c"
            msg = "✓ " + msg
        except Exception as e:
            msg, style = f"✗ {type(e).__name__}: {e}", "#ff8787"
        self.app.call_from_thread(self._tested, msg, style)

    def _tested(self, msg, style):
        self.query_one("#test", Button).disabled = False
        self.query_one("#cred-result", Static).update(Text(msg, style=style))


# --- the app ------------------------------------------------------------------

class TdvmsApp(App):
    TITLE = "tdvms"
    SUB_TITLE = "AFAD/TDVMS campaigns"
    BINDINGS = [
        Binding("f1", "show('campaigns')", "Campaigns"),
        Binding("f2", "show('plan')", "Plan"),
        Binding("f3", "show('mailbox')", "Settings"),
        Binding("q", "quit", "Quit"),
    ]
    CSS = """
    Screen { background: $surface; }
    #ledgers { width: 34; border: round $panel; }
    #detail { width: 1fr; }
    .hint { color: $text-muted; padding: 0 1; height: auto; }
    #overview { padding: 0 1; }
    #chunk-filter { margin: 0 0 1 0; }
    #plan-top { height: 1fr; }
    #map-col { width: 1fr; }
    #map { border: round $panel; }
    #map:focus { border: round $accent; }
    #map-status { height: 1; padding: 0 1; }
    #side { width: 66; border: round $panel; }
    #side-opts { height: auto; }
    #side-opts Checkbox { width: auto; }
    .form-row { height: auto; align-vertical: middle; }
    .form-row Label { padding: 1 1 0 1; }
    .date { width: 16; }
    .num { width: 10; }
    .coord { width: 14; }
    #target { width: 28; }
    #new-name { width: 24; }
    #raw-dir { width: 1fr; }
    #go { margin-left: 1; }
    #preview { height: auto; min-height: 2; padding: 0 1; }
    Confirm, RunCommand, EarthquakePicker { align: center middle; }
    .dialog { width: 76; height: auto; max-height: 90%; border: thick $accent;
              background: $surface; padding: 1 2; }
    .dialog.wide { width: 110; height: 90%; }
    .dialog-title { text-style: bold; padding-bottom: 1; }
    .dialog-body { padding-bottom: 1; }
    .command { background: $panel; padding: 1; margin-bottom: 1; }
    .dialog-buttons { height: auto; align-horizontal: right; }
    .dialog-buttons Button { margin-left: 1; }
    #events { height: 1fr; }
    MailboxView { padding: 1 2; }
    .intro { max-width: 100; padding-bottom: 1; }
    .cred-row { height: auto; max-width: 100; }
    .cred-label { width: 16; padding: 1 1 0 0; }
    .cred-row Input { width: 1fr; }
    .cred-source { padding: 0 0 1 16; }
    #show-pass { margin-left: 15; }
    .cred-buttons { height: auto; padding: 1 0 1 16; }
    .cred-buttons Button { margin-right: 1; }
    #cred-result, #notify-result { padding-left: 16; }
    .section { padding-top: 2; }
    """

    def __init__(self, root):
        super().__init__()
        self.root = root

    def compose(self):
        yield Header()
        with TabbedContent(id="top", initial="campaigns"):
            with TabPane("Campaigns  F1", id="campaigns"):
                yield CampaignsView(self.root)
            with TabPane("Plan  F2", id="plan"):
                yield PlanView(self.root)
            with TabPane("Settings  F3", id="mailbox"):
                yield MailboxView()
        yield Footer()

    def action_show(self, tab):
        self.query_one("#top", TabbedContent).active = tab

    @on(TabbedContent.TabActivated, "#top")
    def _top(self, event):
        if event.pane.id == "plan":
            self.query_one(PlanView).refresh_targets()
            self.query_one("#map", StationMap).focus()


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="tdvms-tui",
        description="Watch and repair TDVMS campaigns, and plan new ones off a map. "
                    "Never starts or stops a run.")
    p.add_argument("--root", default=".",
                   help="the folder holding the ledgers (default: here)")
    args = p.parse_args(argv)
    TdvmsApp(pathlib.Path(args.root).resolve()).run()
    return 0
