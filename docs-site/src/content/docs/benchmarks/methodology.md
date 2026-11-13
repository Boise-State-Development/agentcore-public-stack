---
title: Methodology
description: How the published benchmark is measured, what each number means, and its limits.
sidebar:
  order: 2
---

The benchmark is the platform's evaluation harness, `backend/scripts/eval_harness.py`, run against a deployed stack. Its design is in [`docs/specs/eval-harness.md`](https://github.com/Boise-State-Development/agentcore-public-stack/blob/develop/docs/specs/eval-harness.md); this page is what a reader of the results needs.

## The golden set

- **Cases** live in [`backend/evals/cases/`](https://github.com/Boise-State-Development/agentcore-public-stack/tree/develop/backend/evals/cases), one file per family. Each is a synthetic conversation (one or more turns) with expectations a fixed function checks: values the answer must or must not contain, tools that must or must not be called, a cap on tool calls, a format, a skill that must load.
- **Synthetic by design.** Fixtures are generated documents with planted facts and lookalike distractors. No case uses a real user's data, which is why the results, and the cases a model fails, can be published as they are.
- **Turns run through the deployed product**, the same runtime, tools, skills, compaction and document handling a user's turn goes through, as a dedicated eval account whose role grants exactly the tools the golden set uses.

## What each number is

| Measure | How it is taken |
|---|---|
| Cases passing | Each case runs `k` times (3 for a release). A case passes when a majority of its samples pass every check. |
| Cost per task | The sum of what the platform metered for every model call of a sample, from its per-call cost records, averaged over all samples. Passing and failing samples both count: this is what running the workflow costs. |
| Model calls, tokens | Totals over every model call of a sample, from the same cost records, then the median sample. Input counts uncached, cache-read and cache-written tokens together. |
| Task time | The runtime's own duration of each turn, summed over the sample's turns; median and 90th percentile. |
| First token | The runtime's time to the first streamed token of the sample's first turn; median. |

Token counts come from the cost records rather than the response stream: the stream reports only a turn's last model call, which undercounts a turn that works through tools by two to five times.

## Limits

- **Small samples.** A family of 8 cases moves 12 points on one case. Read counts, not percentages, and treat a one-case difference between models as noise unless it repeats across releases.
- **One environment.** Times are measured on the project's development deployment at the time of the run and move with that day's load. They compare models within a run better than they compare runs.
- **Shared cache writes.** Samples run four at a time, and concurrent samples can share a prompt-cache write, so one sample's cost can be another's saving. The per-task mean over all samples is stable; a single sample's cost is not.
- **Regional prices.** The development deployment uses Regional inference profiles; a production deployment on Global profiles pays about 10% less for the same tokens.
- **What it does not measure.** Answer quality beyond the cases' checks (the judged family is scored by a model and is advisory), behaviour on real users' documents, and anything about a model outside this platform.

## When it runs

Every release runs the whole golden set at `k=3` on each published model, and `eval_harness.py publish` regenerates the document this site renders. A nightly run at `k=1` on the default model watches for regressions between releases; it is not published here.
