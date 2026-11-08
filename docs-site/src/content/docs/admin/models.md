---
title: Models
description: Configure the models offered to users.
sidebar:
  order: 5
---

:::caution[Draft]
This page is a scaffolded placeholder — content to be written.
:::

AI Configuration group. Manage the model catalog (Bedrock, Gemini, OpenAI); backed by the `/admin/manage-models` pages.

## Output limit (Max Output Tokens)

Each model has two output numbers, both on its edit page under **Admin → Models**:

- **Max Output Tokens** (Model capabilities) is the most the model can write in one call. It comes from the model card and is a ceiling, not what is sent.
- The **Max Output Tokens** row under **Inference parameters** holds the **Default** the runtime sends on every call. Agent authors cannot change it: the Agent Designer hides `max_tokens` on purpose, so this default governs every agent and every chat on that model.

When a response reaches the default, it stops there and the chat says it was cut off at the model's output limit. **Continue** sends a follow-up turn and the model carries on from where it stopped. On Claude 4.6 and later models, which do not accept an assistant prefill, the continuation is a new model call: it can restart a sentence or repeat a table header at the join.

A higher default does not make ordinary answers longer or more expensive, because a call bills only the tokens it writes. It raises the cost of the worst case. On Claude Opus 5.5 at $22 per million output tokens, a call that reaches 32,000 tokens costs about $0.70, and one that reaches 64,000 costs about $1.41.

### Raising the default on an existing model

The curated catalog sets a default when a model is first added. A model that is already in your catalog keeps the value it was saved with, so a new release does not change it. In releases after 1.33.0 the curated default for Claude Opus 5.5 is 64,000 (it was 32,000). To bring an existing row up to date:

1. Open **Admin → Models**, then the model (for example Claude Opus 5.5).
2. Expand **Inference parameters**.
3. In the **Max Output Tokens** row, set **Default** to the new value, for example `64000`. It cannot exceed the row's **Max**, which cannot exceed the model's Max Output Tokens.
4. Save. The next turn on that model uses the new default; no deploy or restart is needed.

To update every environment at once, an operator can run `backend/scripts/backfill_model_output_caps.py` instead. It changes only rows still on the old curated default and leaves any row an admin has edited alone. It is a dry run unless you pass `--apply`.

Claude Haiku 5.5 keeps a 32,000 default on purpose. A Haiku 5.5 prompt over 100,000 tokens is billed at 5x, and a long answer is re-sent as input on the next call, so a larger default can push the following call over that line.
