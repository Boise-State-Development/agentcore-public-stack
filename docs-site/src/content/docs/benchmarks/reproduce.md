---
title: Reproduce the benchmark
description: Run the published benchmark on your own deployment and get the same tables.
sidebar:
  order: 3
---

The benchmark runs against any deployment of the stack. You need AWS credentials for the deployment's account and its resource prefix.

## 1. Seed the eval account

Creates the eval users and a role that grants exactly the tools and skills the golden set uses:

```bash
cd backend
uv run python scripts/seed_eval_user.py --prefix <your-prefix> --region <your-region>
uv run python scripts/seed_eval_fixtures.py --prefix <your-prefix> --region <your-region> --base-url https://<your-app>/api
```

## 2. Run it

Estimate first; nothing is spent without `--yes`:

```bash
uv run python scripts/eval_harness.py run --out /tmp/bench-haiku --auth cognito \
  --base-url https://<your-app>/api --prefix <your-prefix> --region <your-region> \
  --model-id us.anthropic.claude-haiku-5-5 --k 3 --workers 4
```

Add `--yes` to run. A full run is 429 samples. On the project's development deployment each took 21 to 25 minutes on 2026-11-13, and cost $0.27 on Haiku 5.5, $4.34 on Sonnet 5 and $9.39 on Opus 5.5.

## 3. Publish

```bash
uv run python scripts/eval_harness.py publish --out /tmp/bench-haiku --out /tmp/bench-sonnet \
  --json benchmarks.json --markdown headline.md
```

`publish` refuses a run that missed any case, a sample whose cost could not be read, and any document that would carry a session id, a user id or your deployment's prefix. The JSON is the same shape this site renders.

## 4. Compare

`eval_harness.py score --out /tmp/bench-haiku` compares your run with the committed baseline for that model, family by family, and prints the efficiency table: model calls, tokens and time per case, against the baseline's.
