---
name: usage-patterns-dashboard
description: >-
  Classify a deployment's most expensive conversations by usage pattern (what
  the user was doing: iterative drafting, browser crawl, batch items, pasted
  corpus…) and publish or refresh the "Conversation Cost Patterns" dashboard
  artifact: pattern spend split by cost mechanism, top conversations with
  console links, a discovery queue for new patterns, the 5-minute cache cliff,
  tool reliability, models, attachments, compaction health and run-over-run
  history. Content-free (no message text, titles or user ids). Use when the user
  asks "what are our most expensive conversations doing", "refresh the cost
  patterns dashboard", "classify expensive conversations", "usage patterns",
  "any new usage patterns", or wants to repeat the cost-pattern analysis.
---

# Conversation Cost Patterns dashboard

Each run pulls the window's per-call cost rows and upload records from the
deployment's DynamoDB (read-only, content-free), builds ten datasets locally,
and refreshes **one** Dashboard artifact so its link and run history persist.

- `scripts/build_datasets.py`: taxonomy rules + every dataset. Stdlib only, run with `python3 -I`.
- `assets/index.html`: the dashboard page. Reads datasets only; data-independent.
- `references/taxonomy.md`: what each pattern means, its lever, and how to add one.

Baseline numbers, findings, parked levers and the data still to collect are in
`docs/one-pagers/conversation-usage-patterns.md`. When a run changes the picture, update it.

It complements the per-conversation diagnoses in
`backend/src/apis/app_api/admin/costs/diagnoses.py`: those say *why* one
conversation cost money; this says *what kind* of usage carries the spend.

## 0. Settle the inputs

| Input | Default | Where it comes from |
|---|---|---|
| AWS profile | ask | The deployment's read-only CLI profile (memory may name it) |
| Table prefix | ask | e.g. `boisestateai-v2` → `<prefix>-sessions-metadata`, `<prefix>-user-file-uploads` |
| Console base URL | ask | The SPA origin; rows link to `<base>/admin/costs/sessions/<id>` |
| Window | last 30 days, ending today (UTC) | The user may name another |

Work in a fresh scratchpad directory: `RAW=<scratch>/cpd/raw`, `OUT=<scratch>/cpd/out`.
Raw scans stay there. Never copy them, or any id from them, into the repo.

## 1. Find or create the dashboard

`Artifact` `action: "list"`, `type: "Dashboard"`, and look for the title
**Conversation Cost Patterns**. If it exists, keep its `url` for every write
below. If not, run `Artifact` `action: "quickstart"` (`intent: "other"`),
publish with the Dashboard `type_url` it names, `title: "Conversation Cost
Patterns"`, `auto_open: "after_first_write"`, and read the type's instructions
it returns. They are the contract for steps 5 and 6.

## 2. Pull the data (read-only)

Run each command **on one line**. A prod read-only guard may refuse multi-line
commands and any scripting runtime on a line that names the prod profile. Only
`scan`/`query` are used. The projections are the privacy boundary: never add
`title`, `displayText`, `citations`, `fileName` or any message field.

```bash
aws dynamodb scan --profile <PROFILE> --table-name <PREFIX>-sessions-metadata --filter-expression "begins_with(SK, :c) AND #t >= :d" --projection-expression "sessionId,userId,#t,messageId,#c,tokenUsage,modelInfo.modelId,cacheStatus,cacheGapSeconds,wastedUsd,prefixFingerprints,toolCalls,documentsAttached,documentTokens,hasDocuments,turnDurationMs,windowRemovedMessages,compactionEvents,turnAgentId,agentSwitched" --expression-attribute-names '{"#t":"timestamp","#c":"cost"}' --expression-attribute-values '{":c":{"S":"C#"},":d":{"S":"<START>"}}' --output json > $RAW/cost_rows.json
```

```bash
aws dynamodb scan --profile <PROFILE> --table-name <PREFIX>-user-file-uploads --filter-expression "begins_with(GSI1PK, :c) AND createdAt >= :d" --projection-expression "GSI1PK,sizeBytes,mimeType,createdAt" --expression-attribute-values '{":c":{"S":"CONV#"},":d":{"S":"<START>"}}' --output json > $RAW/uploads.json
```

`userId` is read only to count distinct users. No dataset carries it. The
cost-row scan grows with traffic (about 200 MB for 64K calls); give it a 10-minute timeout.

## 3. Carry the run history forward

The dashboard's `datasets/runs` document holds the history file. `ArtifactData`
`get` collection `datasets`, doc `runs`. If it exists, its `source.url` is
`/_blob/<asset id>`: `Artifact` `action: "read"`, `url`, `path: "<asset id>"`,
and copy the saved file to `$RAW/runs_previous.json`. No document means this
is the first run. Skip `--history`.

## 4. Build

```bash
python3 -I scripts/build_datasets.py --cost-rows $RAW/cost_rows.json --uploads $RAW/uploads.json --history $RAW/runs_previous.json --window-start <START> --window-end <END> --console-base <BASE> --out $OUT
```

Then add lifetime cost and message counts for the listed conversations (session
rows, no title). List the ids in a command that does not name the profile:

```bash
python3 -I -c "import json,sys;print('\n'.join(r['session'] for r in json.load(open(sys.argv[1])) if not r['session'].startswith('api-converse')))" $OUT/conversations.json > $RAW/meta_ids.txt
```

```bash
mkdir -p $RAW/meta; while read sid; do aws dynamodb query --profile <PROFILE> --table-name <PREFIX>-sessions-metadata --index-name SessionLookupIndex --key-condition-expression "GSI_PK = :p AND GSI_SK = :m" --projection-expression "sessionId,totalCost,messageCount,deleted,createdAt" --expression-attribute-values "{\":p\":{\"S\":\"SESSION#$sid\"},\":m\":{\"S\":\"META\"}}" --output json > "$RAW/meta/$sid.json"; done < $RAW/meta_ids.txt
```

Re-run the build with `--meta-dir $RAW/meta` added. It prints the run summary
and the pattern table. Read them before publishing: a fleet total far from the
admin dashboard's figure for the same window means the scan or window is wrong.

## 5. Upload the datasets

`$OUT/manifest.json` lists the ten datasets (`id`, `title`, `description`, `file`).
Upload each file as an asset, one call per JSON file (the ten calls are
independent, so send them together):
`Artifact` `action: "publish"`, `url`, `file_path: $OUT/<file>`, `asset: true`.
Write the replies' urls to `$OUT/assets.json` as `{"<dataset id>": "/_blob/<id>", …}`.

## 6. Write the dashboard

```bash
python3 -I scripts/store_docs.py --manifest $OUT/manifest.json --assets $OUT/assets.json --page assets/index.html --by "<the user's name>" --docs $OUT/docs
```

It writes one JSON file per document (`dash/meta`, every `datasets/<id>`,
`files/index.html`) and prints the `writes` list. Pass that list to one
`ArtifactData` `action: "batch"` on the dashboard `url`. On a refresh the
documents already exist: first `ArtifactData` `action: "list"` the `datasets`,
`dash` and `files` collections, and add each document's `version` as
`if_version` on its entry, or the batch is refused.

Before overwriting, note the `/_blob/<id>` urls the old `datasets/*` documents
pointed to. After the batch commits, delete those assets (`Artifact`
`action: "delete"`, `url`, `path: <asset id>`), since nothing names them now. Follow the
Dashboard type's own rule on verification: re-run nothing beyond what it names.

## 7. Report

In chat, from the build summary and the datasets, never from titles:

1. Window, spend, and change since the previous run (`runs`).
2. The three costliest engineerable patterns (not Quick Q&A or Unclassified), their mechanism mix, and lever.
3. Movement: any pattern whose share moved 2+ points since the last run.
4. **Discovery queue**: read `conversations` rows with `queue` containing `discovery`.
   If five or more share a feature signature (similar tools, uploads, paste size,
   calls/turn, cadence), propose a new pattern with a rule. Follow
   `references/taxonomy.md`: add it to `PATTERNS`, bump `RULES_VERSION`, rebuild,
   and confirm the queue shrank without moving other patterns' spend unexpectedly.
5. Diagnostics worth acting on: a tool over 20% errors, a model with unpriced calls,
   a jump in idle re-write share.

Give the dashboard link. Others can't open it until the user shares it.
