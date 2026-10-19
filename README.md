# tdvms

**Bulk-download seismic waveform data from AFAD's TDVMS portal: unattended,
resumable, and safe to leave running for days.**

[TDVMS](https://tdvms.afad.gov.tr) serves continuous waveform data from
Turkey's seismic networks. It doesn't return data directly. Each request goes
into a queue, and a download link arrives **by email**, usually within an hour
or a few. The portal handles **one request at a time per email address**. A year
of data for a dozen stations therefore means hundreds of requests, hundreds of
emails and days of waiting.

tdvms automates that loop. You tell it which stations and which dates you want.
It splits the request into windows, submits them in parallel from several
addresses, reads the replies from your inbox, downloads and checks each archive,
and files it by station and date. It also notices and repairs a request that
stalls. Stop it at any point and start it again later: it picks up where it
left off.

- **A terminal UI** for picking stations on a map, or around an earthquake, and
  watching a download as it runs.
- **A command-line tool** for scripting, servers and long unattended runs.
- **Station and earthquake catalogs included**, so you can plan without a
  network connection.
- **Self-repairing.** Lost links, a portal that answers "busy", stations that
  never return data and dropped mail connections are all handled without you.

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [Quick start](#quick-start)
- [Concepts](#concepts)
- [The terminal UI](#the-terminal-ui)
- [Command-line reference](#command-line-reference)
- [Running for days](#running-for-days)
- [Mailbox setup](#mailbox-setup)
- [Catalogs](#catalogs)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Requirements

- **Python 3.12+** and [uv](https://docs.astral.sh/uv/).
- **An email account with IMAP access that supports plus-addressing.** That
  means mail sent to `you+a1@…`, `you+a2@…` and so on all arrives in `you@…`.
  Gmail does this. A mailbox used only for this tool is the easiest setup.
- **Disk space.** Broadband stations come to roughly 0.5 GB per station per
  three weeks of data, with wide variation between stations.
- Linux. macOS works too, but the terminal UI can't see which downloads are
  running there, because it finds them through `/proc`.

## Install

```bash
git clone <repository-url> tdvms
cd tdvms
uv sync --extra tui
```

`uv sync --extra tui` installs the command-line tool and the terminal UI. Plain
`uv sync` installs the command-line tool alone. That's useful on a server: the
UI's dependency is optional, and the downloader never needs it.

Every command below is run as `uv run <command>` from the project folder. If you
activate the environment first (`source .venv/bin/activate`), plain `tdvms` and
`tdvms-tui` work too.

## Quick start

**1. Open the terminal UI.**

```bash
uv run tdvms-tui
```

**2. Set up the mailbox (F3).** Enter your IMAP server, address and password,
press **Test connection**, then **Save**. For Gmail, use `imap.gmail.com` and an
[app password](https://myaccount.google.com/apppasswords), not your account
password.

**3. Plan a download (F2).** Move around the map and press `space` on the
stations you want. Or press `e` to choose an earthquake: every station within
the radius is picked and the dates are set around the event. Name the campaign
and press **Plan**.

**4. Start it.** The UI shows the exact command, ready to copy:

```bash
setsid nohup uv run tdvms run --ledger campaigns/van-2011/ledger.jsonl \
    --out-dir campaigns/van-2011/raw --address you@gmail.com --slots 8 \
    >> campaigns/van-2011/run.log 2>&1 &
```

**5. Watch it (F1).** Progress, the slots in use, every window and the live log.
Archives arrive in `campaigns/van-2011/raw/<STATION>/<STATION>_<date>.zip`.

The same without the UI:

```bash
export TDVMS_IMAP_HOST=imap.gmail.com TDVMS_IMAP_USER=you@gmail.com
export TDVMS_IMAP_PASS='<app password>'

uv run tdvms plan --ledger elba.jsonl --station ELBA --start 2024-01-01 --end 2024-07-01
uv run tdvms run  --ledger elba.jsonl --out-dir elba_raw --address you@gmail.com --slots 8
```

## Concepts

**Campaign.** One download job: a set of stations over a date range. It lives in
a single file, the *ledger*. The terminal UI keeps each campaign in its own
folder under `campaigns/`.

**Window (chunk).** A campaign is split into windows of one station over a fixed
span, 21 days by default. Each
window is one request and one archive. A window is named `STATION:START`, for
example `ELBA:2024-01-01`.

**Ledger.** A plain-text file, one JSON line per window, recording where each
window stands. It is the campaign's only state. Delete everything else and the
ledger still knows what's done. Stop the program mid-download and the next run
carries on from it.

**Slots.** The portal handles one request per email address at a time. tdvms
gets around that with plus-addresses: `--address you@gmail.com --slots 8` makes
`you+a1@gmail.com` through `you+a8@gmail.com`. That's eight separate queue
places, all delivering to one inbox. More slots means more windows in flight.

**Window states.**

| state | meaning |
|---|---|
| `pending` | waiting to be requested |
| `claimed` | being submitted right now |
| `submitted` | accepted by the portal; waiting for the email |
| `fetched` | downloaded, verified and filed |
| `nodata` | the portal has no data for this window |
| `failed` | gave up after repeated failures |
| `retired` | not requested, because its station is known not to answer |

## The terminal UI

```bash
uv run tdvms-tui                 # campaigns in the current folder
uv run tdvms-tui --root ~/quakes # or somewhere else
```

The UI shows and repairs campaigns but **never starts or stops a download**.
That stays in your hands, and a download keeps running if you close the UI. It
is safe to leave open next to a running campaign.

### Campaigns (F1)

Every campaign, with its progress, and a ● when a download is running.

| tab | shows | keys |
|---|---|---|
| Overview | totals per state and per station, turnaround times, anything stuck | |
| Slots | each address, the window it holds and for how long | |
| Chunks | every window; type to filter by station, state, slot or note | `r` requeue the selected window |
| Log | the running download's output, live | |
| Strays | archives a download kept but couldn't match to its own campaign | `i` file it into the campaign that wants it, `d` delete a duplicate |

### Plan (F2)

A map of every station next to a searchable list. The two stay in step.

| key | action |
|---|---|
| arrows or `h` `j` `k` `l` (with shift: faster) | move the cursor |
| `+` `-` or the mouse wheel | zoom |
| `space`, `enter` or a click | pick or unpick the station under the cursor |
| `0` | show the whole map |
| `e` | **earthquake mode**: pick an event, a radius and the months either side |
| `x` | clear the earthquake marker |
| `c` | clear all picks |

Stations are coloured by network: **TU** (AFAD), **KO** (Kandilli) and **TB**.
The **TK** strong-motion network (accelerometers rather than broadband
seismometers) is behind a toggle, off by default. Below the
map, set the start and end dates and the window length, and choose an existing
campaign or a new one. You can put a new campaign's downloads on another disk.
The preview shows exactly how many windows the plan adds before you confirm.

### Mailbox (F3)

The three mailbox settings, where each one currently comes from (the environment
or the saved file), a **Test connection** button, and **Save**. See
[Mailbox setup](#mailbox-setup).

## Command-line reference

```
tdvms <command> [options]          tdvms <command> --help for every option
```

| command | does |
|---|---|
| `plan` | add a station's windows to a ledger |
| `run` | the downloader: submit, receive, download, repeat |
| `status` | summarise a ledger, including anything stuck |
| `reset` | put one window back in the queue |
| `ingest` | file an archive that is already on disk |
| `adopt` | import a ledger from the older `afad_campaign` tool |

Every command takes `--ledger PATH`, which defaults to `tdvms_ledger.jsonl` in
the current folder.

### `tdvms plan`

Adds one station's windows to a ledger. Run it once per station. Running it
again is always safe: it never adds a window the ledger already has, so
extending `--end` is the way to add more time.

```bash
tdvms plan --ledger my.jsonl --station ELBA --start 2023-01-01 --end 2024-01-01
```

| option | default | |
|---|---|---|
| `--station CODE` | required | station code as the portal lists it, e.g. `ELBA`; a network prefix like `TU.` is dropped |
| `--start DATE`, `--end DATE` | `2024-05-01`, `2026-08-10` | the date range, `YYYY-MM-DD` |
| `--chunk-days N` | `21` | window length |
| `--no-verify` | | skip checking the code against the portal's station list |

### `tdvms run`

The downloader. Each cycle it reads the inbox and downloads whatever has
arrived, frees the slots those requests held, repairs anything stalled, then
fills every free slot with the oldest waiting window. Stations take turns, so
one big station can't starve the others. It runs until you stop it.

```bash
tdvms run --ledger my.jsonl --out-dir my_raw --address you@gmail.com --slots 8
```

| option | default | |
|---|---|---|
| `--address EMAIL` | required | your **base** address; slots are made from it |
| `--slots N` | `8` | how many plus-addresses to use at once |
| `--out-dir DIR` | `afad_raw` | where archives go: `DIR/<STATION>/<STATION>_<date>.zip` |
| `--station CODE` | all | only request these stations (repeatable) |
| `--interval SECONDS` | `60` | pause between cycles |
| `--once` | | run one cycle, print the summary, exit |
| `--dry-run` | | show what a cycle would do; changes nothing anywhere |
| `--timeout SECONDS` | `180` | how long to wait for the portal to accept a request |

Mailbox options:

| option | default | |
|---|---|---|
| `--folder NAME` | `INBOX` | the IMAP folder the portal's mail lands in |
| `--search CRITERIA` | `(UNSEEN)` | which messages to read. In a shared mailbox, narrow it, e.g. `'(UNSEEN SUBJECT "TDVMS")'` |
| `--claim-unknown` | off | also act on mail to plus-addresses this ledger never used |

Self-repair options:

| option | default | |
|---|---|---|
| `--claim-timeout SECONDS` | `600` | requeue a submission that died midway |
| `--submit-timeout SECONDS` | `21600` | requeue a request whose email never came (6 h) |
| `--max-attempts N` | `3` | give up on a window after this many tries |
| `--busy-cooldown SECONDS` | `1800` | rest a slot after the portal says it's still busy |
| `--nodata-streak N` | `8` | stop requesting a station after this many no-data answers and no data ever |
| `--no-retire` | | warn about such a station but keep requesting it |

### `tdvms status`

```bash
tdvms status --ledger my.jsonl
tdvms status --ledger my.jsonl --address you@gmail.com --slots 8   # plus each slot
```

Counts per state and per station, data volume, turnaround times, and anything
stuck, each with its age. A station that has never returned data is flagged.

### `tdvms reset`

```bash
tdvms reset --ledger my.jsonl --chunk ELBA:2024-01-01
tdvms reset --ledger my.jsonl --station ELBA --start 2024-01-01 --note "re-request"
```

Puts one window back to `pending`. A date that matches windows at more than one
station is refused, with the matches listed, so a reset never lands somewhere
you didn't mean.

### `tdvms ingest`

```bash
tdvms ingest --ledger my.jsonl --file downloads/some_archive.zip --out-dir my_raw
```

Files an archive you already have, for example one you downloaded by hand from a
link. The window is read from the archive's own contents. The file is never
deleted if it can't be matched. `--force` replaces an archive already filed for
that window.

### `tdvms adopt`

```bash
tdvms adopt --ledger my.jsonl --from old_campaign_ledger.jsonl
```

Imports a ledger written by the older `afad_campaign` tool. The source file is
only read, never changed.

## Running for days

Start the downloader detached, so it outlives your terminal:

```bash
setsid nohup uv run tdvms run --ledger my.jsonl --out-dir my_raw \
    --address you@gmail.com --slots 8 >> my_run.log 2>&1 &
```

A `tmux` or `screen` session works just as well.

- **Stopping.** `kill -INT <pid>` or `kill -TERM <pid>` (or Ctrl-C in a
  terminal). Nothing is lost: anything in flight is picked up by the next `run`.
- **One download per address.** Two downloads using the same base address would
  share slots and read each other's mail. A second one is refused, with a
  message naming the first.
- **Several campaigns.** Run them one after another on the same address, or side
  by side on *different* base addresses. Different addresses can share one
  mailbox safely: mail for a slot a download didn't use is left unread for its
  owner.
- **Throughput.** The portal works through requests at its own pace. Expect
  replies within one to a few hours. More slots means more windows in flight at
  once.

## Mailbox setup

tdvms needs three settings:

| variable | example |
|---|---|
| `TDVMS_IMAP_HOST` | `imap.gmail.com` |
| `TDVMS_IMAP_USER` | `you@gmail.com` |
| `TDVMS_IMAP_PASS` | an app password |

Set them in either of two ways:

- **In the terminal UI (F3).** They're saved to `~/.config/tdvms/mailbox.env`,
  readable only by you. You can also `source` that file in a shell.
- **As environment variables**, by hand, from a `.envrc`, or from a secrets
  manager.

When both are set, the **environment wins**, setting by setting. `AFAD_IMAP_*`
names are accepted too. Nothing else is stored.

**Gmail:** turn on 2-step verification, create an
[app password](https://myaccount.google.com/apppasswords), and use it as
`TDVMS_IMAP_PASS`.

tdvms only marks mail as read after it has dealt with it. A download that
fails is also flagged (starred), so you can find it.

## Catalogs

Shipped with the package in `src/tdvms/data/`:

| file | contents | source |
|---|---|---|
| `stations.csv` | every station the portal lists: code, network, location, instruments | the TDVMS portal |
| `events.csv` | M5+ earthquakes since 1990 across the station area | [USGS](https://earthquake.usgs.gov/fdsnws/event/1/), public domain |
| `outline.json` | borders and coastlines for the map | [Natural Earth](https://www.naturalearthdata.com/), public domain |

To refresh the station and earthquake lists:

```bash
uv run python -m tdvms.catalog
```

Notes:
- A few station codes (`ALT`, `KULU`, `MADM`) exist in two networks. Ledgers
  identify stations by code, and `run` requests the TU one.
- Place names are as USGS publishes them, which shows some Turkish letters as
  `?`.

## Troubleshooting

**Nothing seems to happen after submitting.** That's normal for the first hour
or so: the portal queues requests and mails links later. `status`, or the
Overview tab, shows each waiting request and how long it has waited.

**"missing mailbox credentials".** Set them on the Mailbox tab (F3), or export
the three variables. See [Mailbox setup](#mailbox-setup).

**"the portal does not list 'XXXX'".** The code isn't in the portal's station
list. The error lists the nearest codes.

**`[BUSY]` in the log.** The portal still holds an earlier request for that
address. The slot rests for `--busy-cooldown` and carries on; nothing to do.

**A station keeps answering "no data".** After `--nodata-streak` such answers,
with nothing ever downloaded, it stops being requested (`retired`). Data may
simply not exist for it in that period.

**`UNMATCHED_*.zip` files in the download folder.** Archives for windows this
campaign doesn't have, usually because two campaigns shared a mailbox. The
Strays tab files each one into the campaign that wants it.

**"already being worked".** Another download is using that address. Stop it
first, or use a different base address.

## Development

```bash
uv sync --extra tui
uv run pytest
```

The portal and the mailbox are passed into the download loop as arguments, so
the tests run it against fakes with no network at all. Most tests are named
after a failure seen in real use. The terminal UI's logic is tested without a
terminal, and its screens are driven headless. Those tests are skipped when the
`tui` extra isn't installed.

```
src/tdvms/
  cli.py         `tdvms <command>`; each command is its module's main()
  run.py         the downloader       supervisor.py   one cycle of it
  ledger.py      the ledger           slots.py        slots and the per-address lock
  client.py      the portal           mailbox.py      the inbox
  credentials.py mailbox settings     fetch.py        download, verify, file
  plan.py  report.py  reset.py  ingest.py  adopt.py   the other commands
  catalog.py     the catalogs         data/           stations, events, map outline
  tui/           the terminal UI: model.py (logic), mapview.py (map), app.py (screens)
```

Waveform data comes from AFAD. Follow AFAD's terms of use, and credit the data
source in work that uses it.
