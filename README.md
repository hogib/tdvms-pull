# tdvms

An AFAD/TDVMS download campaign that supervises itself.

TDVMS emails a download link rather than returning data, and processes one
request at a time **per email address**. A campaign is therefore hundreds of
round trips spread over days, and the only thing that survives interruption is a
ledger recording exactly which `(station, window)` chunks are done.

```bash
uv run tdvms plan   --station ELBA --start 2024-05-01 --end 2026-08-10
uv run tdvms run    --address you@gmail.com --slots 8
uv run tdvms status --address you@gmail.com --slots 8
```

## Why this exists

The campaign tool this replaces worked, but it was three programs — a submitter,
a mailbox poller, and a CLI the poller *shelled out to* for every state change.
Submit, fetch and free-the-slot were three processes, and every stall the
campaign produced lived in the seams between them. Nine of those are on the
record, all observed on a live pull, and each one is now a named regression test:

| what happened | where it is pinned |
|---|---|
| `reset --start <date>` matched every station in the file | `test_a_date_matching_two_stations_is_refused` |
| a `claimed` row never timed out; one held a slot for a day and a half | `test_a_claim_that_died_mid_post_is_reclaimed` |
| a locally-`failed` station's slots still answered `[111] BUSY` | `test_a_portal_busy_backs_the_chunk_out_and_cools_the_slot` |
| plus-addresses typed by hand drifted to four naming schemes | `test_a_pool_is_one_address_and_a_count` |
| the claim took the first pending line in the **file**, not the oldest window | `test_claim_takes_the_oldest_window_not_the_first_line` |
| five no-data answers freed five slots and refilled none | `test_a_nodata_answer_frees_its_slot_and_the_slot_is_refilled` |
| a reset kept a stale `fetched_at`; status printed `range -3064-76 min` | `test_stamps_that_invert_are_dropped_rather_than_carried` |
| a station answered no-data 26 times out of 26 and kept taking slots | `test_a_station_that_never_returns_data_stops_taking_slots` |
| a ~60 s disconnect was read as a failure, though the request was accepted | `test_an_unconfirmed_submission_keeps_its_claim` |

## The loop

`run` is the campaign. One process owns the ledger, the mailbox and the portal
client, so a state change and the slot release it implies happen in the same
locked write. Each cycle:

1. **reap** — read the inbox, download and verify each link, record the outcome,
   free the slot.
2. **reclaim** — release slots held against requests that will not answer. Ten
   minutes for a `claimed` row, six hours for a `submitted` one.
3. **retire** — stop submitting for a station with no fetches and a long run of
   no-data answers.
4. **submit** — fill every genuinely free slot, oldest window first,
   round-robin across stations.

`reap` runs first so a slot freed now is filled in step 4 of the same cycle
rather than a tick later. `submit` runs last, once the free list is as long as it
is going to get.

## Two states per slot

A slot's *local* state is what the ledger says it holds. Its *remote* state is
what the portal last said to it. They disagree more often than is comfortable:
marking a chunk `failed` clears the local state and frees nothing at the portal.
A slot is submittable only when both agree.

## Migration

```bash
uv run tdvms adopt --from ../cnn_earthquake/afad_campaign_ledger.jsonl
```

The source is opened read-only and never written, so the old campaign can keep
running while the two ledgers are compared.

## Mailbox

```bash
export TDVMS_IMAP_HOST=imap.gmail.com
export TDVMS_IMAP_USER=you@gmail.com
export TDVMS_IMAP_PASS='<app password, not the account password>'
```

`AFAD_IMAP_*` are accepted too, so a live environment does not have to be
re-exported mid-campaign. Credentials come from the environment only; nothing is
written to disk but the ledger.

One mailbox can serve several campaigns. A link addressed to a slot this ledger
never submitted from is left **unread**, not consumed — otherwise whichever
poller ticks first burns a link its owner is still waiting for.

## Tests

```bash
uv run pytest
```

The portal and the mailbox are injected into `supervisor.cycle`, so the loop is
tested as a pure function of `(ledger, inbox, portal)` with no network at all.
