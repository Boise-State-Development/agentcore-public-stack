# Usage-pattern taxonomy

Two axes, kept apart on purpose:

- **Pattern**: what the user was doing. Exactly one per conversation; the first rule in
  `PATTERNS` (in `scripts/build_datasets.py`) that matches wins, so order is precedence.
- **Modifier**: a trait that cuts across patterns. Any number per conversation.

The per-conversation diagnoses in `admin/costs/diagnoses.py` are a third, separate axis:
the mechanism that made one conversation expensive. A pattern names a workload you can
design for; a diagnosis names a defect you can fix.

Every rule reads only content-free features computed from per-call cost rows and upload
records: tool ids and counts, token counts, timings, model ids, upload type and size.

## Patterns, in precedence order

| Pattern | Rule | Why it costs money | Lever |
|---|---|---|---|
| API pipeline | `api-converse-*` session | High call volume; cost stored without a breakdown | Visibility: these sessions are missing from the console's conversation list |
| Browser crawl | 25+ browse/fetch, or 10+ at 6+ calls/turn | Dozens of model calls per turn, each re-reading the whole context (reads dominate) | Browsing sub-agent with its own context; per-turn step budget |
| Batch items | 15+ uploads, ≥1 per 3 turns | Independent items in one thread; each rides along for every later item | Batch mode with a fresh context per item |
| Spreadsheet loop | 15+ spreadsheet tool calls | Code Interpreter probe loops and retries | Bound results; fix the error rate |
| Workspace assembly | 10+ Drive/Canvas/Google calls | Many file reads into context; connector search errors | Fix search errors; offload large reads |
| Iterative drafting | 3+ `update_artifact` or 6+ artifact writes | Full-document rewrite as output on every edit, plus a superseded copy in history | Patch-style edits; drop superseded versions from history |
| Document study | ≥1 MB uploaded | Document in the cached prefix, re-written on every idle return | Document offload and digests |
| Screenshot support | 4+ image uploads | Images re-sent on later calls | Drop images after their turn |
| Web research | 10+ search/browse calls | Search payloads accumulate | Bound and summarise results |
| Office generation | 3+ Word/PowerPoint calls | Large generated payloads | None yet |
| Long-form chat | 10+ turns, almost no tools | Context grows; user pauses between turns | Smaller context at re-entry |
| Pasted corpus | 40K+ token jump on a turn with no attachment or tool result | Pasted text sits in the prefix forever, outside the document path | Paste-to-attachment in the composer |
| Quick Q&A | under 10 turns and under $0.50 | Cheap by nature | None |
| Unclassified | no match | Discovery queue | Read it; promote recurring signatures |

## Modifiers

| Modifier | Rule |
|---|---|
| Over 100K context | peak context ≥ 100K tokens |
| Slow cadence | ≥ 8 turns and ≥ half after a 5+ minute pause |
| Large paste | the pasted-corpus signal in a conversation with no uploads |
| Tool loop | ≥ 5 model calls per turn |
| Premium model | ≥ half of calls on Opus, Fable or GPT-6 |
| Multi-week | spans 14+ days or 5+ active days |

## Feature definitions that are easy to get wrong

- **Turn**: a call more than 60 s after the previous one. Tool rounds inside a turn are seconds apart.
- **Idle re-write**: cache-write cost on a call more than 300 s after the previous one. The
  Bedrock cache TTL is 5 minutes, and the dashboard's pause chart shows the cliff.
- **Paste jump**: on a call whose predecessor issued no tools and which carries no attachment,
  context growth minus the predecessor's own output tokens. For a first call, growth over the
  p10 first-call context of conversations with the same `toolConfigHash`, because a large tool
  set alone puts 40K+ tokens on an empty conversation. Conversations with uploads are excluded
  from the modifier, because document rehydration looks the same.
- **`prefixTokens.tools`** on cost rows is an estimator residual and known to be unreliable.
  No rule uses it.

## Adding a pattern from the discovery queue

1. Find five or more discovery-queue conversations with a shared signature. Use the
   conversations dataset or the session profile endpoint
   (`GET /admin/costs/sessions/{id}/profile`), which is content-free.
2. Write the rule from features `features()` already computes, or add a feature there
   (content-free only).
3. Insert it into `PATTERNS` ahead of any broader rule it should win against. Give it a
   label, a one-sentence definition, and a lever stated as an engineering change.
4. Bump `RULES_VERSION`. The run history records it, so a share change caused by a rule
   change is distinguishable from a real shift in usage.
5. Rebuild, then check that the discovery queue shrank and that no other pattern moved
   in a way the new rule doesn't explain. Add a row to the table above.
