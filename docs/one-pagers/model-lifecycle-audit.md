# Model lifecycle audit — Bedrock, not Anthropic

**Run:** 2026-09-02 · us-west-2 · `aws bedrock list-foundation-models`
**Why:** kaizen review 2026-08-28, proposal #2 (part 2). Anthropic deprecated
`temperature` / `top_p` / `top_k` on Claude Opus 4.7 and later, which prompted
the question of what *else* about our model ids is on a clock we aren't reading.

## The correction this audit exists to make

**Read Bedrock's schedule, not Anthropic's.** Partner platforms set their own
dates; an Anthropic deprecation notice is evidence about the API, not about the
Bedrock model id we actually invoke. The two have diverged before and will again.

## What Bedrock actually publishes

A **status, not a date**: `modelLifecycle.status` is `ACTIVE` or `LEGACY`. There
is no retirement date in the API, so the registry cannot surface a countdown —
only "still current" vs "on the way out". `LEGACY` is the signal to migrate;
treat its appearance as the notice period, because that is all there is.

```bash
aws bedrock list-foundation-models --region us-west-2 --by-provider anthropic \
  --query 'modelSummaries[].{id:modelId,status:modelLifecycle.status}' --output table
```

## Result — all four curated models are current

| Curated key | Bedrock model id | Status |
|---|---|---|
| `claude-opus-4-7` | `anthropic.claude-opus-4-7` | ACTIVE |
| `claude-sonnet-5` | `anthropic.claude-sonnet-5` | ACTIVE |
| `claude-sonnet-4-6` | `anthropic.claude-sonnet-4-6` | ACTIVE |
| `claude-haiku-4-5` | `anthropic.claude-haiku-4-5-20251001-v1:0` | ACTIVE |

`LEGACY` in us-west-2 as of this run: Sonnet 4 (`20250514`), Opus 4.1, and the
Claude 3 Haiku family. **We curate none of them**, so there is no migration
pending and no action falls out of this audit.

## Standing note

Newer ids (Opus 4.7/4.8/5, Sonnet 5, Fable 5.x) carry **no dated suffix** —
`anthropic.claude-opus-4-7`, not `...-20251101-v1:0`. Version pinning by date is
no longer available on those, so "which snapshot am I on" stops being answerable
from the id. Re-run this query when adding a model, and again if a turn starts
failing in a way that looks like a model changed underneath us.
