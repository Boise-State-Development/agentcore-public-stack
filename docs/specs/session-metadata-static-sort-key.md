# Session-metadata static sort key (issue #175)

**Status:** proposed (spec only — no branch yet)
**Supersedes the band-aids for:** `SessionMetadata` parse-failure warnings, first-turn
duplicate rows, and the documented `update_session_activity` race window.
**Code touched:** `backend/src/apis/shared/sessions/metadata.py`,
`backend/src/apis/app_api/sessions/services/session_service.py` (the **live**
soft-delete — see below), `infrastructure/lib/constructs/data/cost-tracking-tables-construct.ts`.

## Problem

The session-metadata row encodes `lastMessageAt` **inside its sort key**:

```
PK = USER#{user_id}
SK = S#ACTIVE#{lastMessageAt}#{session_id}
```

In DynamoDB a key is immutable identity, so "the session had activity" — a value
that changes every turn — cannot be an in-place update. It forces a **row move**:
`put_item` at the new SK + `delete_item` at the old
([`update_session_activity`](../../backend/src/apis/shared/sessions/metadata.py) Phase B, ~line 1018).

Two whole classes of bug fall out of that single decision:

1. **Ghost rows / parse failures.** ~20 other writers (`_bump_session_aggregates`,
   the `REMOVE pausedTurn` / `REMOVE lastTurnContinuable` / `REMOVE lastTurnInterrupted`
   paths, title/starred/tags/interrupt writers) resolve the SK via a GSI read, then
   issue a targeted `update_item`. If a concurrent activity-update **rotates the SK
   away** between that read and the write, the write lands on a now-deleted key —
   and `update_item` on a missing key **upserts**. A `REMOVE` on a missing key still
   creates the item, so you get a bare `{PK, SK}` **ghost** with none of the 6
   required fields. It fails `SessionMetadata.model_validate` and is skipped on
   list ([metadata.py:1794-1804](../../backend/src/apis/shared/sessions/metadata.py)),
   emitting `Failed to parse session item`. Observed in prod: 5 ghosts / ~15k rows,
   47 warnings/day. Harmless today, but it is a **lost write** (the intended
   `REMOVE`/`SET` never applied to the real row) — e.g. a `pausedTurn` that should
   have been cleared can persist.

2. **First-turn duplicate rows.** Because every call computes a *different* SK,
   `ensure_session_metadata_exists` cannot use a real `attribute_not_exists(PK)`
   conditional put — the condition is always vacuously true at a never-before-seen
   key ([metadata.py:749-756](../../backend/src/apis/shared/sessions/metadata.py)).
   Two concurrent first turns for one session mint two rows.

Both are symptoms of one disease: **the row moves, and writers race a moving
target.** Condition-guarding each writer (`attribute_exists(SK)`) and reaping
ghosts on read are O(N-writers) ongoing patches that leave the lost-write and
duplicate-row problems in place. This spec removes the cause.

## Decision summary

| Question | Decision |
|----------|----------|
| Root change | **Make the sort key static** — `SK = S#{session_id}`, no timestamp |
| Where does `lastMessageAt` live | A **plain attribute** on the row (already present) |
| How is active-vs-deleted expressed | The existing **`status`** attribute, not an SK prefix |
| How is recency listing served | A **new sparse GSI** `SessionRecencyIndex` keyed on `USER#{id}` / `{lastMessageAt}#{session_id}` |
| Reuse `UserTimestampIndex`? | **No** — it's owned by per-message cost records; co-mingling sessions into that hot partition is muddy. Dedicated sparse GSI is cleaner and cheaper. |
| Sparse how | GSI keys written **only when `status == active`**; soft-delete `REMOVE`s them so the row drops out of the active listing automatically (mirrors `DueScheduleIndex`/GSI3 pattern already in this table) |
| Migration shape | **Forward-only strangler**: writers self-migrate their row on next touch + a one-shot backfill for cold rows |
| Ghost cleanup | Backfill deletes existing ghosts; no reaper needed long-term (rotation gone → new ghosts structurally impossible) |

## Target schema

```
Session row (one per session, active OR deleted):
  PK      = USER#{user_id}
  SK      = S#{session_id}                     ← STATIC. never rotates.
  GSI_PK  = SESSION#{session_id}   GSI_SK = META      (SessionLookupIndex — unchanged)
  GSI4_PK = USER#{user_id}         GSI4_SK = {lastMessageAt}#{session_id}
                                                (SessionRecencyIndex — sparse, active-only)
  status  = "active" | "archived" | "deleted"
  lastMessageAt, createdAt, title, messageCount, ... (all as today)
```

- `S#` prefix retained so session rows stay distinguishable from `C#` cost /
  `D#` records in the same `USER#{id}` partition.
- `GSI4_SK` suffixes `#{session_id}` to disambiguate identical `lastMessageAt`
  values and give a stable pagination cursor.
- On soft-delete / archive: `SET status=... REMOVE GSI4_PK, GSI4_SK` → the row
  leaves `SessionRecencyIndex` (sparse) but the base row stays for direct lookup
  and any deleted-view. On restore: re-add GSI4 keys.

### CDK addition (`cost-tracking-tables-construct.ts`)

```ts
this.sessionsMetadataTable.addGlobalSecondaryIndex({
  indexName: 'SessionRecencyIndex',
  partitionKey: { name: 'GSI4_PK', type: dynamodb.AttributeType.STRING },
  sortKey:      { name: 'GSI4_SK', type: dynamodb.AttributeType.STRING },
  projectionType: dynamodb.ProjectionType.ALL,   // list needs the full item
});
```

(GSI3 is `DueScheduleIndex`; GSI4 is the next free slot. DynamoDB allows 20.)

## What each path becomes

**Reads**
- `list_user_sessions` / `_list_user_sessions_cloud` — the only substantive read
  change. Was: base-table `query(PK, begins_with SK 'S#ACTIVE#', ScanIndexForward=False)`.
  Becomes: `query(SessionRecencyIndex, GSI4_PK='USER#{id}', ScanIndexForward=False)`.
  Same newest-first ordering + native pagination, now from an index whose sort key
  is *allowed* to track a mutating attribute (DynamoDB re-positions the index entry
  when `lastMessageAt` is `SET` — no move, no race).
- `_get_session_by_gsi` (SessionLookupIndex) — **unchanged**, already SK-agnostic.
- **Bonus:** with a deterministic SK, callers holding both `user_id` and
  `session_id` can `get_item(PK=USER#{id}, SK=S#{id})` directly and skip the GSI
  read entirely (optional follow-up, not required for correctness).

**Writes** (all ~20 in `metadata.py`)
- Every `update_item` now targets the **stable** `S#{session_id}` → pure in-place
  update. No rotation ⇒ no upsert-on-missing ⇒ **ghosts structurally impossible**,
  **no lost writes**, **no condition guards required anywhere**.
- `update_session_activity` loses Phase B entirely: it becomes a single
  `SET lastMessageAt, preferences ADD messageCount` on the static SK. DynamoDB
  updates `GSI4_SK` automatically.
- `ensure_session_metadata_exists` uses a **real** `put_item(ConditionExpression=
  "attribute_not_exists(PK)")` on the deterministic key → genuinely idempotent,
  killing the first-turn duplicate race.
- Soft-delete stops moving `S#ACTIVE#`→`S#DELETED#`; instead
  `SET status='deleted' REMOVE GSI4_*` on the static SK. **The live soft-delete is
  `SessionService.delete_session`** (`app_api/sessions/services/session_service.py`,
  used by the `DELETE /{session_id}` route), which today reconstructs
  `old_sk = S#ACTIVE#{last_message_at}#{id}` from a read-back `last_message_at` and
  transactionally moves to `S#DELETED#`. That reconstruction is itself racy (a
  concurrent activity-update changes `last_message_at`, so the rebuilt `old_sk` is
  stale) — and it **vanishes** under the static SK (the key is just `S#{id}`).
  `shared/sessions/metadata.py`'s `_store_session_metadata_cloud` active→deleted
  branch (~line 649) is the other builder. **Note:**
  `app_api/sessions/services/metadata.py` is a **dead duplicate** (zero production
  importers — only one test references `_update_cost_summary_async`); confirm and
  delete it as part of this work rather than migrating it.

## Migration (forward-only strangler)

Changing a base-table SK means every existing row must be rewritten (delete+put) —
inherently a full migration. Kept safe and online via an **expand → migrate →
backfill → contract** sequence. The split between 1a and 1b is load-bearing: no row
may start migrating until *every* running container can already read both schemes,
or an old container mid-deploy would list legacy-only and a just-migrated row would
briefly vanish from a user's sidebar.

**Phase 0 — infra.** Deploy CDK adding `SessionRecencyIndex`. No behavior change;
index is empty until rows gain GSI4 keys. (`platform.yml`.)

**Phase 1a — expand read (deploy).** `metadata.py` that **reads** both schemes:
`list_user_sessions` returns the UNION of the new `SessionRecencyIndex` query + the
legacy `begins_with('S#ACTIVE#')` base query, deduped by `session_id`. Writes still
use the legacy scheme; **no row migrates yet.** New sessions may already be written
in target shape (they're visible via the union regardless). This deploy must be
**fully rolled out** — every task on 1a — before 1b begins.

**Phase 1b — migrate writes (deploy).** Now that all readers cope with both schemes,
turn on conversion: on any write, if the GSI-resolved SK is legacy
(`S#ACTIVE#…`/`S#DELETED#…`), perform the row's **final** rotation to `S#{id}` +
populate/clear GSI4, then apply the write. Each write self-migrates its row once.
Ghost creation ends for every migrated row (in-place updates). Hot sessions convert
themselves from here on.

**Phase 2 — backfill.** One-shot throttled script (below) migrates the cold tail that
1b traffic didn't touch, and deletes existing ghosts. Idempotent; PITR (already
enabled on this table) is the rollback net. Sets the migration-complete marker once a
full scan finds zero legacy rows.

**Phase 3 — contract.** `list_user_sessions` goes GSI-only once a persisted
"migration complete" marker is set (the backfill sets it after confirming zero legacy
rows). The legacy union branch and self-migration shim are **retained behind that
marker**, not hard-deleted, so a downstream fork that hasn't run the backfill stays
safely in dual-read rather than losing sight of un-migrated rows. See
[Downstream / forked deployments](#downstream--forked-deployments). A later release
can drop the legacy code entirely once all known deployments report the marker set.

### Backfill script sketch (`backend/scripts/`)

```
scan sessions-metadata (paginate, throttled)
for each item:
  if SK starts_with 'S#ACTIVE#' or 'S#DELETED#':
    if is_ghost(item):                 # no title/status/GSI_SK=META
        delete_item(PK, SK); continue
    sid = session_id from item (or parse tail of SK)
    new = {**item, 'SK': f'S#{sid}',
           'status': 'deleted' if SK.startswith('S#DELETED#') else item['status']}
    if new['status'] == 'active':
        new['GSI4_PK'] = item['PK']; new['GSI4_SK'] = f"{item['lastMessageAt']}#{sid}"
    else:
        new.pop('GSI4_PK', None); new.pop('GSI4_SK', None)
    put_item(new, ConditionExpression=attribute_not_exists(SK) OR SK == new.SK)
    if new['SK'] != item['SK']: delete_item(PK, item['SK'])   # drop legacy row

# final pass: if a full scan finds no remaining S#ACTIVE#/S#DELETED# legacy rows,
# set the "migration complete" marker that flips list_user_sessions to GSI-only
if no_legacy_rows_remain():
    put_item({'PK': 'MIGRATION#session-sk', 'SK': 'STATE', 'complete': True})
```

Run against **prod-ai** and **dev-ai**. ~15k rows → minutes at modest WCU.
Idempotent and re-runnable; the marker write is what advances Phase 3.

## Downstream / forked deployments

This is a public stack; forks run their own tables and data and upgrade by pulling.
The migration must be safe for them without assuming they read an upgrade guide or
run a script by hand.

**Fresh deployments — zero risk.** A new install has no legacy rows: sessions are
born in target shape, the `SessionRecencyIndex` GSI ships in their CDK, nothing to
migrate. Only the normal `platform.yml`-before-`backend.yml` ordering applies.

**Existing deployments — safe except at one boundary.** Deploying the expand/migrate
code onto legacy data is safe: dual-read shows legacy rows, writers self-migrate on
touch, cold rows keep working. The one hazard is **contract (Phase 3) reached without
the backfill having run** — cold, un-migrated sessions have no GSI4 keys and vanish
from the GSI-only list. This is **invisibility, not loss** (rows persist, PITR
recovers, a late backfill makes them reappear), but it's an unacceptable upgrade
surprise. A `git pull` never runs the backfill, and `develop`→`main` squash-merges
can land all phases in one release, so git ordering alone cannot protect a forker.
Three protections make the migration self-defending:

1. **Gate contract on a persisted "migration complete" marker — do not hard-delete
   the legacy read path.** Keep the union/legacy fallback in code, guarded by a flag
   that the backfill sets **only after it confirms zero legacy rows remain**. Store
   the marker as a sentinel item in the table (e.g. `PK=MIGRATION#session-sk`,
   `SK=STATE`) or an SSM param. Effect: a fork that never migrates simply stays in
   dual-read forever — correct, just slightly less tidy — and **cannot brick its
   sidebar by pulling across the boundary**. The maintainer flips to GSI-only by
   virtue of the marker being set; forkers do so automatically once their own data
   is converted. This is the single most important protection.
2. **Degrade gracefully when the GSI is absent.** If a fork runs only `backend.yml`
   and the new list query hits `SessionRecencyIndex` before the CDK created it,
   DynamoDB raises `ResourceNotFoundException`. Catch it and fall back to the legacy
   base-table query, so a missed infra deploy is a soft degradation, not a broken
   session list.
3. **Optional: ship the backfill as an auto-run migration.** A CDK custom-resource
   Lambda (or a run-once, lock-guarded startup migration in app-api) converts cold
   rows with no human step. More moving parts; the marker gate (1) already prevents
   the dangerous outcome, so this is a nicety, not a requirement.

**Preconditions to document** (`RELEASE_NOTES.md`, via the `cutting-a-release` flow):
- PITR is the safety net and is on in the shipped CDK
  (`pointInTimeRecoveryEnabled: true`); forks that disabled it lose the backstop.
- Deploy `platform.yml` (GSI) with/before `backend.yml`.
- Prefer spreading the phases across **tagged releases** so downstream upgrades cross
  one boundary at a time; if bundled, the marker gate (1) still holds.

## Pagination token

Current token = `base64(lastMessageAt)` tied to the old SK
([metadata.py:1666](../../backend/src/apis/shared/sessions/metadata.py)). New token =
`base64(json(GSI4 LastEvaluatedKey))`. Make the decoder **tolerant**: an
undecodable/legacy token falls back to no-cursor (first page) rather than erroring —
so in-flight tokens spanning the Phase 1a→3 deploys degrade to a harmless page reset,
never a 500.

## Testing (moto-backed, `tests/…/sessions/`)

1. **No rotation** — `update_session_activity` twice ⇒ SK unchanged; exactly one
   row for the session; `lastMessageAt`/`messageCount` advanced.
2. **Ghost impossible** — simulate the race (resolve SK, then activity-update, then
   a `REMOVE` write) ⇒ no `{PK,SK}`-only stub; the `REMOVE` applied to the real row.
3. **Idempotent create** — two concurrent `ensure_session_metadata_exists` ⇒ one row.
4. **Recency + pagination** — N sessions, bump middle one ⇒ it sorts to front via
   `SessionRecencyIndex`; token round-trips; tolerant decode of a legacy token.
5. **Soft-delete drops from index** — delete ⇒ absent from recency query, base row
   still `get_item`-able; restore re-adds.
6. **Backfill** — seed legacy rows + a ghost ⇒ script migrates rows, deletes ghost,
   is idempotent on re-run.

## Risks / open questions

- **`SessionRecencyIndex` partition heat** — `GSI4_PK = USER#{id}`; per-user session
  counts are modest, no hot-partition concern (same cardinality as today's base query).
- **Deleted/archived listing — RESOLVED, none exists.** Verified: the list endpoint
  (`GET /sessions`) takes only `limit`/`next_token`, no status filter; `"archived"`
  status is never written (dead enum value); `S#DELETED#` is a **write-only tombstone**
  — no code path ever queries or `begins_with('S#DELETED#')` it; and the SPA has no
  trash/archive/deleted-sessions view. So the sparse active-only `SessionRecencyIndex`
  is fully sufficient and **Phase 3 is unblocked** — no second GSI or scan path needed.
  A soft-deleted row keeps its static SK + `GSI_SK=META`, so it stays directly
  retrievable for the memory/files purge fan-out.
- **Backfill vs live traffic** — Phase 1b self-migration means the script mostly
  handles cold rows; the conditional put + legacy-SK-only delete avoids clobbering a
  concurrently-migrated row. PITR is the backstop.
- **GSI eventual consistency** — list can be ~sub-100ms stale, identical to today's
  GSI-resolved writes; acceptable.

## Appendix — before / after item

```
BEFORE (rotates every turn; races spawn ghosts)
  PK  USER#u123
  SK  S#ACTIVE#2026-07-08T17:33:08.442948+00:00#c13e1dfd     ← moves each turn
  GSI_PK SESSION#c13e1dfd   GSI_SK META
  title, status=active, lastMessageAt, ...

AFTER (stable identity; recency via sparse GSI)
  PK  USER#u123
  SK  S#c13e1dfd                                             ← never moves
  GSI_PK  SESSION#c13e1dfd   GSI_SK  META
  GSI4_PK USER#u123          GSI4_SK 2026-07-08T17:33:08.442948+00:00#c13e1dfd
  title, status=active, lastMessageAt, ...
```
