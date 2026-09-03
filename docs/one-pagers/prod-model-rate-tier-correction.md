# Prod runbook — correct the managed-model rate tier

**Applies to:** prod (`beta.boisestate.ai`, acct 897729136999, us-west-2, prefix `boisestateai-v2`)
**Origin:** kaizen 2026-08-28 #1 → PR #914. Dev was corrected 2026-09-03; prod was not.
**Expected duration:** ~10 minutes, all through the admin API.

## What is wrong

Every curated Claude template declared **Global**-tier prices while its `modelId` named a
**`us.*` Regional (CRIS)** inference profile, which prices ~10% higher. Managed-model rows are
seeded from those templates, so prod's rows are almost certainly a flat ~10% low on all four
rate fields — including whichever model is `isDefault`.

PR #914 fixed the template. It did **not** touch existing rows, which is why this runbook exists.

Confirmed in dev: **all four** Claude rows were wrong, including a hand-created
`us.anthropic.claude-sonnet-5` row that did not match the curated `global.anthropic.claude-sonnet-5`
template at all. Do not assume prod's row set matches dev's — discover before writing.

## Two things to know before you start

**1. The Regional premium is not a universal 1.1×.** It holds for Haiku 4.5, Sonnet 4.5/4.6,
Sonnet 5 and the Opus 4.5–5 family, but **Claude Sonnet 4 prices identically on both tiers**
(3.00/15.00 either way). Never apply a blanket multiplier — read the per-model tier row below.

**2. Do NOT touch historical cost rows.** `create_pricing_snapshot` stamps every model call with
the rates in force at the time, so past `C#` rows are internally consistent and comparable.
Rewriting them would corrupt the time series the whole cost-effectiveness arc is measured on.
This is a fix-forward change: new calls price correctly, old calls stay as billed.

## Verified rates — us-west-2, AWS Price List API, published 2026-09-01

Per 1M tokens. Cache write is **1.25 × input**, cache read is **0.1 × input** on every model
(1-hour-TTL write, which we do not use, is 2×). Pick the row matching your `modelId` prefix:
`global.*` → Global, anything else → Regional.

| Model | Tier | input | output | cache write | cache read |
|---|---|---|---|---|---|
| Haiku 4.5 | **Regional** | 1.10 | 5.50 | 1.375 | 0.11 |
| Haiku 4.5 | Global | 1.00 | 5.00 | 1.25 | 0.10 |
| Sonnet 4 | either | 3.00 | 15.00 | 3.75 | 0.30 |
| Sonnet 4.5 | **Regional** | 3.30 | 16.50 | 4.125 | 0.33 |
| Sonnet 4.5 | Global | 3.00 | 15.00 | 3.75 | 0.30 |
| Sonnet 4.6 | **Regional** | 3.30 | 16.50 | 4.125 | 0.33 |
| Sonnet 4.6 | Global | 3.00 | 15.00 | 3.75 | 0.30 |
| Sonnet 5 | **Regional** | 2.20 | 11.00 | 2.75 | 0.22 |
| Sonnet 5 | Global | 2.00 | 10.00 | 2.50 | 0.20 |
| Opus 4.5 / 4.6 / 4.7 / 4.8 / 5 | **Regional** | 5.50 | 27.50 | 6.875 | 0.55 |
| Opus 4.5 / 4.6 / 4.7 / 4.8 / 5 | Global | 5.00 | 25.00 | 6.25 | 0.50 |
| Fable 5 / 5.1, Mythos 5.1 | **Regional** | 11.00 | 55.00 | 13.75 | 1.10 |
| Fable 5 / 5.1, Mythos 5.1 | Global | 10.00 | 50.00 | 12.50 | 1.00 |

Legacy families (Claude 3.x, Opus 4/4.1, Instant) are **deliberately omitted** — re-verify those
from the Price List API if prod carries one. Re-verify anything with:

```bash
aws pricing get-products --region us-east-1 \
  --service-code AmazonBedrockFoundationModels \
  --filters Type=TERM_MATCH,Field=regionCode,Value=us-west-2
```

Newer ids publish `*_tokens_standard` usagetypes; older ones publish `*TokenCount`. A query
written for one shape silently returns nothing for the other.

## Procedure

Run steps 1–3 in the **browser devtools console on `https://beta.boisestate.ai`**, signed in as a
models admin. The admin API is the right surface here: it validates the payload, updates
`updatedAt`, and applies a partial update (`exclude_none=True`) so untouched fields — role grants,
`isDefault`, `enabled`, `supportedParams` — are preserved. Writing to DynamoDB directly bypasses
all of that; don't.

### Step 1 — Discover (read-only, writes nothing)

```js
const RATES = {
  'haiku-4-5':  { regional: [1.1, 5.5],  global: [1.0, 5.0] },
  'sonnet-4-6': { regional: [3.3, 16.5], global: [3.0, 15.0] },
  'sonnet-4-5': { regional: [3.3, 16.5], global: [3.0, 15.0] },
  'sonnet-4':   { regional: [3.0, 15.0], global: [3.0, 15.0] },
  'sonnet-5':   { regional: [2.2, 11.0], global: [2.0, 10.0] },
  'opus-4-5':   { regional: [5.5, 27.5], global: [5.0, 25.0] },
  'opus-4-6':   { regional: [5.5, 27.5], global: [5.0, 25.0] },
  'opus-4-7':   { regional: [5.5, 27.5], global: [5.0, 25.0] },
  'opus-4-8':   { regional: [5.5, 27.5], global: [5.0, 25.0] },
  'opus-5':     { regional: [5.5, 27.5], global: [5.0, 25.0] },
  'fable-5':    { regional: [11.0, 55.0], global: [10.0, 50.0] },
  'mythos-5':   { regional: [11.0, 55.0], global: [10.0, 50.0] },
};
const round = n => Math.round(n * 1e6) / 1e6;
// Longest key first so `sonnet-4-6` never matches the `sonnet-4` entry.
const familyOf = id => Object.keys(RATES).sort((a,b) => b.length - a.length).find(k => id.includes(k));

const res  = await fetch('/api/admin/managed-models', { credentials: 'include' });
const data = await res.json();
const rows = Array.isArray(data) ? data : (data.models ?? data.items ?? []);

window.__planned = [];
console.table(rows.map(m => {
  const id = m.modelId ?? '';
  const fam = familyOf(id);
  const tier = id.startsWith('global.') ? 'global' : 'regional';
  const cur = [m.inputPricePerMillionTokens, m.outputPricePerMillionTokens,
               m.cacheWritePricePerMillionTokens, m.cacheReadPricePerMillionTokens];
  if (!fam) {
    return { modelId: id, tier, verdict: m.provider === 'bedrock' ? 'UNKNOWN — check by hand' : 'skip (non-Bedrock)',
             current: cur.join(' / '), expected: '', emptySpec: '' };
  }
  const [i, o] = RATES[fam][tier];
  const want = m.supportsCaching ? [i, o, round(i*1.25), round(i*0.1)] : [i, o, cur[2], cur[3]];
  const ok = JSON.stringify(cur) === JSON.stringify(want);
  if (!ok) window.__planned.push({
    id: m.id, modelId: id,
    body: {
      inputPricePerMillionTokens: want[0], outputPricePerMillionTokens: want[1],
      ...(m.supportsCaching ? { cacheWritePricePerMillionTokens: want[2],
                                cacheReadPricePerMillionTokens: want[3] } : {}),
    },
  });
  return {
    modelId: id, tier, default: !!m.isDefault,
    current: cur.join(' / '), expected: want.join(' / '),
    verdict: ok ? 'ok' : 'NEEDS FIX',
    emptySpec: Object.keys(m.supportedParams?.params ?? {}).length === 0 ? 'EMPTY SPEC — see step 4' : '',
  };
}));
console.log(`${window.__planned.length} row(s) queued for update`);
```

**Read the output before continuing.** Every row should be `ok` or `NEEDS FIX`. Any
`UNKNOWN — check by hand` is a Bedrock model not in the table above — price it from the Price List
API and handle it separately; the apply step deliberately skips it rather than guessing.

### Step 2 — Apply

```js
const csrf = document.cookie.split('; ').find(c => c.startsWith('__Host-bff_csrf='))
  ?.split('=').slice(1).join('=');
if (!csrf) throw new Error('No CSRF cookie — are you signed in?');

for (const p of window.__planned) {
  const r = await fetch('/api/admin/managed-models/' + p.id, {
    method: 'PUT', credentials: 'include',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
    body: JSON.stringify(p.body),
  });
  console.log(p.modelId, r.status, r.ok ? 'updated' : await r.text());
}
```

Note the endpoint keys on the record's **`id` UUID**, not `modelId` — passing `modelId` 404s.
A missing `X-CSRF-Token` gives a 403, not a silent failure.

### Step 3 — Verify

Re-run **Step 1**. Every Bedrock row should read `ok`. Then spot-check the invariant:

```js
const d = await (await fetch('/api/admin/managed-models', {credentials:'include'})).json();
(Array.isArray(d) ? d : (d.models ?? d.items ?? []))
  .filter(m => m.supportsCaching)
  .forEach(m => console.log(m.modelId,
    'cw=1.25x:', Math.abs(m.cacheWritePricePerMillionTokens - m.inputPricePerMillionTokens*1.25) < 1e-9,
    'cr=0.1x:',  Math.abs(m.cacheReadPricePerMillionTokens  - m.inputPricePerMillionTokens*0.1)  < 1e-9));
```

Send one chat turn afterwards and confirm the per-session cost still renders — that exercises
`get_model_pricing` → `create_pricing_snapshot` against the new values.

### Step 4 — Empty `supportedParams` (separate decision, don't bundle)

Any row Step 1 flagged `EMPTY SPEC` is **not protected by PR #915**. That change makes an omitted
param mean *unsupported*, but only for rows that declare a spec at all — a row declaring nothing
has made no claim, so it keeps the old permissive pass-through. On a model that deprecates
`temperature` / `top_p` / `top_k` (Opus 4.7 and later, Sonnet 5) a stale client override can still
reach Bedrock and hard-400 the turn mid-stream.

Fix is data, not code. Seed the spec, scoping `max_tokens.max` to that row's **actual**
`maxOutputTokens` rather than the curated template's:

```js
const csrf = document.cookie.split('; ').find(c => c.startsWith('__Host-bff_csrf='))
  ?.split('=').slice(1).join('=');
const RECORD_UUID = '<id from step 1>';
const MAX_OUT     = 4096;   // <- read this off the row; do not guess

await fetch('/api/admin/managed-models/' + RECORD_UUID, {
  method: 'PUT', credentials: 'include',
  headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
  body: JSON.stringify({ supportedParams: { params: {
    max_tokens: { supported: true, min: 1, max: MAX_OUT, default: MAX_OUT },
    effort: { supported: true, allowed: ['low','medium','high','xhigh'], default: 'medium' },
  }}}),
});
```

This narrows what users can send, so send one turn on that model afterwards to confirm it still
streams. Done on dev for `us.anthropic.claude-sonnet-5` with no issues.

## Rollback

Step 1 prints the `current` values before anything is written — **screenshot or copy that table
first**. To revert, PUT the original four numbers back to the same record UUID. Nothing else is
touched, and no historical data is modified at any point, so there is nothing else to undo.

## After

- Cost figures for prod rise ~10% for affected models. That is the *correction*, not a regression:
  we were under-reporting. Expect `wastedUsd`, `partialMissUsd`, per-session cost and the admin
  cost anatomy to step up at the cutover, and annotate the date so the discontinuity is legible
  rather than looking like a spend spike.
- Worth a follow-up, separately: `global.*` profiles price ~9% **below** `us.*` CRIS on every field
  and we already run one, so the mechanism is proven — but it needs a data-residency review nobody
  has done. Kaizen #1 explicitly said not to bundle it with the rate fix.
