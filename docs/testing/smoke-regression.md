# Smoke and regression testing — the turn path and everything it emits

**Audience:** any agent or developer about to merge a change, and the release
driver before a tag. Written so it can be followed without the rest of this
repo's history: each layer says what it proves, what it needs, how to run it,
and how to read a failure.

**Scope:** the chat turn path end to end — `POST /chat/stream` on app-api,
the AgentCore Runtime `/invocations` behind it, every SSE event type in
CLAUDE.md's table, the interrupt/resume round trips, persistence and restore,
and the SPA surfaces that render them. It does not cover admin CRUD, the
marketplace, knowledge-base ingestion or voice; those have their own specs.

The three layers are cumulative. Run as many as the change warrants (§4).

| Layer | What it proves | Cost | Where |
|---|---|---|---|
| **L0 Hermetic** | the branch's code compiles and its unit/contract tests pass | free, ~8 min backend | `pytest`, `ng test`, `jest` |
| **L1 Turn matrix (API/SDK)** | the deployed or local stack streams every response code path with the right frames, resumes, 409s, restores | real model calls, cents | `backend/scripts/smoke_turns.py` |
| **L2 Browser** | the SPA renders what L1 proved the wire carries: consent, question picker, Stop, steer, attachment cards, reload | real model calls, minutes of agent time | in-app browser against dev |

---

## 1. L0 — hermetic suites

Backend, from the branch's checkout. `PYTHONPATH` is not optional in a
worktree: the shared venv's editable install points at the main checkout, so a
green run without it validates `develop`, not the branch.

```bash
cd <checkout>/backend
PYTHONPATH=$PWD/src AWS_EC2_METADATA_DISABLED=true \
  <main checkout>/backend/.venv/bin/python -m pytest tests/ -q
```

Confirm once per worktree that the import resolves to the branch:

```bash
PYTHONPATH=$PWD/src <main checkout>/backend/.venv/bin/python -c "import apis.shared; print(apis.shared.__file__)"
```

The suite's socket guard fails any test that reaches the network, and that
teardown assert is load-bearing: the fail-open production code swallows the
raised error, so a test that "passes" while hitting AWS is a test whose
assertion never ran. Do not fix a stall with dummy AWS credentials; that turns
the silent fail-open into real failures in unrelated files.

Frontend and infrastructure:

```bash
cd frontend/ai.client && npx ng test --watch=false
```

```bash
cd infrastructure && npx tsc --noEmit && npx jest
```

The unit tests that pin the turn-path invariants, worth running by name when a
change touches the stream: `tests/routes/test_inference.py`,
`tests/apis/inference_api/test_preparing_phase.py`,
`tests/agents/main_agent/test_prompt_cache_determinism.py`,
`tests/agents/main_agent/streaming/`.

---

## 2. L1 — the turn matrix

`backend/scripts/smoke_turns.py` drives real turns through the same
`POST /chat/stream` the SPA uses and asserts the wire contract. One run, one
table, exit 1 on any FAIL.

### 2.1 Targets

| Target | Command | Covers | Needs |
|---|---|---|---|
| **Local stack** | `uv run python scripts/smoke_turns.py --base-url http://127.0.0.1:8000` | everything incl. 409 / Stop / steer | app-api on :8000 with `SKIP_AUTH=true` in `backend/src/.env`, inference-api on :8001. `SKIP_AUTH_ROLES=system_admin` to read cache rows. Local code, **dev data**. |
| **Deployed, through app-api** | `SMOKE_USERNAME=… SMOKE_PASSWORD=… uv run python scripts/smoke_turns.py --base-url https://<host>/api --auth cognito` | everything | a Cognito user with a permanent password on a Hosted UI pool (the nightly pipeline's E2E users qualify; managed login v2 cannot be scripted) |
| **Deployed, straight at the Runtime** | `AWS_PROFILE=dev-ai uv run python scripts/smoke_turns.py --auth headless-grant --user-id <sub> --prefix dev-boisestateai-v2` | frame contract, resume, restore, cache rows | an **active headless grant** for `--user-id` (enable headless runs / "Run now" once while signed in; lapses 30 days after that login) and an AWS profile for the account. Skips the app-api-only rows; reads and deletes in-process through the shared services. |

Run from `backend/` so `apis.shared` imports. The script refuses a target that
looks like production unless `--allow-prod` is passed; the prod account also
has a read-only guard, and nothing here is worth running there.

Every run spends real model dollars as the user it runs as and writes real
session rows. Prompts are a few words, the model is the user's default unless
`--model-id` says otherwise, and the sessions it created are deleted at the
end unless `--keep`. `--delete-sessions a,b` cleans up after a kept or
crashed run without sending a turn.

### 2.2 The rows

Status meanings: **PASS/FAIL** assert a contract. **SKIP** means the row
cannot run here, usually a missing fixture flag, and is never a failure.
**OBSERVED** rows report what happened and never fail, because the behaviour
is a race by design. **WARN** lines under a row are model-quality or
new-event-name notes that do not change the status.

| Row | Turn shape | Asserts | Fixture |
|---|---|---|---|
| `plain_first_turn` | one short prompt, no tools | I1 frame order: only `quota_*`/`agent_notice`/`citation`/`agent_status`/`session_title` before `message_start`; deltas after it; `message_stop` then only tail frames; `metadata` before `done`; `done` once and last; `session_title` ≤1 and never "New Conversation"; no `stream_error`; every JSON frame parses | — |
| `second_turn_cache` | second prompt, same session | I1 again; then `GET /admin/costs/sessions/{id}/calls`: last row not `partial_miss`, `toolConfigHash`/`systemPromptHash` equal across the two rows. `uncached` is normal for prompts this small. Warns if `agent_status.preparing` fired on a warm session | admin scope to read the rows; otherwise PASS on the frame contract alone |
| `restore_after_reload` | `GET /sessions/{id}/messages` | both user prompts and both streamed assistant texts come back byte-for-byte; ≥2 messages per turn | — |
| `tool_turn` | prompt forcing `--tool` (default `calculator`) | a `tool_use` and a `tool_result`; `agent_status` carries `tool_start`/`tool_end` when present; warns if the arithmetic is wrong | the tool granted to the user |
| `ask_user_question_resume` | prompt forcing `ask_user_question` | `user_question_required` **after** the final `message_stop` with `interruptId` + `questions`; resume with `{answers: {<header>: {selected, text}}}` finishes without re-raising; `pendingInterrupts` empty afterwards | `ASK_USER_QUESTION_ENABLED` (default on), tool granted |
| `tool_approval_resume` | prompt forcing `--approval-tool` | `tool_approval_required`, resume with `"approved"`, a `tool_result` follows | `--approval-tool <id>` for a tool flagged `needsApproval` (dev has an MCP server whose hello tool is) |
| `duplicate_send_409` | second POST while the first streams | HTTP 409 with the single-flight detail | app-api target |
| `stop_mid_answer` | abort the stream after 12 deltas, `POST /sessions/{id}/interrupt {reason: user_stopped}` | the next send is accepted within 45 s (lease released); reports whether the partial answer persisted. A short turn can finish server-side before the heartbeat and clear its own marker, which is by design | app-api target |
| `preview_session` | `preview-…` session id | completes; `GET /messages` empty or 404; absent from `GET /sessions` | — |
| `steer_mid_turn` | three-tool prompt, `POST /sessions/{id}/steer` on the first `tool_use` | OBSERVED: whether `steering_applied` landed or the steer lost the race (then #916's end-of-turn flush sends it as a normal turn). FAIL only if the endpoint errors; SKIP on 404 (flag off) | app-api target, `MID_TURN_STEERING_ENABLED` |
| `attach_pdf` | inline PDF generated in-script | completes; the restored user message carries a document block or the attachment marker; warns if the token was not read back | `--with-attachments` |
| `attach_csv` | inline CSV | completes; restored user text carries `[Attached files: …]` (diverted, not inlined) | `--with-attachments` |
| `skill_invoke` | `enabled_skills` + `invoked_skills` | the `skills` tool is called | `--skill-id` of a granted skill |
| `kb_agent_first_turn` | `rag_assistant_id` | any `citation` frames precede `message_start`; warns if none | `--agent-id` of an agent with a KB |
| `quota_exceeded` | plain prompt as an exhausted user | `stopReason=quota_exceeded`, zero call rows | `--expect-quota-exceeded`, a test tier at its limit |

The report's `agent_status` counts include a `preparing`/`prepared` pair on
**every** turn: the backend emits them unconditionally and the SPA delays the
render, so their presence is not a cache miss. The `prepared.durationMs` is;
the `second_turn_cache` row warns when it exceeds 200 ms on a warm session.

**Observed-only events** are counted at the bottom of the report and never
asserted, because nothing outside the runtime can trigger them on demand:
`model_retry` (Bedrock throttling), `compaction` (needs a long history or a
threshold override), `tool_group_summary` (side-channel timing),
`steering_applied` (race). The report also prints client-clock first-token
latency per run; compare it against `turn_prelude` lines (turn-path-ttft §6C
R1), never against a different day's run.

**Reading a failure.** The detail names the frame or field that broke the
contract. Three traps that look like product bugs:

- `tool_turn` with no `tool_use`: the tool is not granted to the user, or the
  model declined. Check `GET /tools` for the user before filing anything.
- `second_turn_cache` unreadable: the admin endpoint 403'd. Locally that is
  `SKIP_AUTH_ROLES=DotNetDevelopers`; set it to `system_admin`.
- `stop_mid_answer` says the partial answer did not persist: the turn finished
  server-side inside the heartbeat window and the coordinator cleared the
  marker on purpose. Lengthen nothing; it is the documented control case.

### 2.3 Adding a row

A new SSE event or turn shape gets a row here, a line in CLAUDE.md's SSE table,
and a unit test that pins it. In the script: write `row_<name>(ctx)` returning
a `RowResult`, raise `RowFailure` for a contract breach and `RowSkip` when a
fixture is missing, append it to `ROWS`, and add the event name to
`KNOWN_EVENTS` so it stops surfacing as a WARN.

---

## 3. L2 — the in-app browser

For the rows that need a rendered surface. The in-app browser pane shares the
user's signed-in session on `https://dev.boisestate.ai`, so this runs against
the deployed artifact with real RBAC. Prefer `read_page` / `read_network_requests`
over screenshots; drive the composer with `form_input` and click
`button[aria-label="Submit message"]`.

**Frame capture.** The SPA's SSE client resolves `window.fetch` at call time,
so wrapping it timestamps every frame without touching the app:

```js
(() => {
  window.__frames = [];
  const real = window.fetch;
  window.fetch = async (input, init) => {
    const res = await real(input, init);
    const url = typeof input === 'string' ? input : input.url;
    if (!url.includes('/chat/stream') || !res.body) return res;
    const t0 = performance.now();
    const [a, b] = res.body.tee();
    (async () => {
      const r = a.getReader(); const dec = new TextDecoder(); let buf = '';
      for (;;) { const {value, done} = await r.read(); if (done) break;
        buf += dec.decode(value, {stream: true});
        let i; while ((i = buf.indexOf('\n\n')) >= 0) {
          const frame = buf.slice(0, i); buf = buf.slice(i + 2);
          const m = frame.match(/^event:\s*(\S+)/m);
          window.__frames.push({ms: Math.round(performance.now() - t0), name: m ? m[1] : '?', frame});
        } }
    })();
    return new Response(b, {status: res.status, headers: res.headers});
  };
})();
```

Read `window.__frames.map(f => f.ms + ' ' + f.name)` after the turn. Any
non-GET call made by hand from the pane needs the `X-CSRF-Token` header mirrored
from the `__Host-bff_csrf` cookie, or it 403s.

**Checklist** (each row: do it, confirm the expectation, reload the page,
confirm the conversation restores with the same content):

| Row | Do | Expect |
|---|---|---|
| First turn | send a plain prompt in a new conversation | loader phases, then text; sidebar title replaces "New Conversation" during or right after the answer |
| Second turn | send again | no "Getting ready…" label (warm build); `second_turn_cache` in L1 covers the hashes |
| Agent with KB | pick an agent, ask about its corpus | citation chips render; answer uses the corpus |
| `@`-mention | mention an agent in a plain thread, then send a plain follow-up | the mention turn runs as that agent; the follow-up sees the mention's messages |
| Attach PDF / CSV / PPTX | attach, send | PDF inline; CSV and PPTX diverted with the guidance note; cards survive reload |
| Skill | enable a skill, invoke it with `/` | the skills tool call renders; the answer follows the skill |
| OAuth MCP tool | use a connector tool before consenting | Connect affordance after the answer; consent; the same turn resumes (not a new one) |
| Ask-user-question | prompt as in L1 | picker renders with Other + Skip; answering resumes; Skip resumes |
| Tool approval | call the approval-gated tool | Approve banner; one click resumes |
| Stop | send a long prompt, click Stop mid-answer | partial text stays; interrupted chip; the next send is accepted, no "already responding" |
| Steer | send a multi-tool prompt, type a follow-up and Enter while it streams | either the follow-up is injected at a tool boundary or sent as the next turn; never lost |
| Continue after `max_tokens` | force a long answer with a low max-tokens model setting | the continuation extends, does not restart |
| Quota exceeded | as a user on an exhausted test tier | conversational refusal, persisted, no spinner |
| Duplicate send | double-submit quickly | the SPA queues rather than erroring; a raw second POST 409s |
| Dark mode and mobile | toggle theme, `resize_window` mobile | tool rails, pickers and banners keep contrast and do not overflow |

Driving dev writes real data for the signed-in user. Delete the smoke
conversations afterwards from Manage Conversations.

---

## 4. When to run what

| Change touches | Run |
|---|---|
| anything | L0 for the packages touched |
| `inference_api/chat/*`, `agents/main_agent/**`, `apis/shared/harness/**`, the SSE table | L0 + L1 against dev once the branch is deployed there (merge to `develop` auto-deploys dev), or against a local stack before |
| an interrupt flow (OAuth, approval, question, browser login) | + L1's resume rows + the matching L2 row |
| the composer, loader, tool rail, pickers, banners | + L2 rows for the surface |
| a release | L0 + full L1 against dev + the L2 checklist, once, on the release branch |

Model-call-path changes also owe the prompt-cache and TTFT evidence described
in CLAUDE.md ("Token cost effectiveness", "TTFT is a hard budget");
`second_turn_cache` and the first-token line in L1's report are the cheap
first look, not the whole proof.

---

## 5. Where this runs in CI

- The PR gate runs L0 path-scoped (`.github/workflows/`, see #1392).
- The nightly pipeline runs the health smoke (`scripts/nightly/smoke-test.sh`)
  and, with `run-e2e`, the Playwright suite (`frontend/ai.client/e2e`) against
  an ephemeral stack. L1 is a natural addition to that job: the E2E users have
  permanent passwords, so `--auth cognito` works there. Wire it as a
  non-blocking step first and promote it once it has been green for a week.
- L2 is agent-run, not CI.

---

## 6. Related

- `docs/specs/turn-path-ttft.md` §6 — the invariants (I1–I3), the per-change
  checklist and the dev recipes (`turn_prelude` logs, cache rows, A/B). Its
  former §6D matrix now lives here as §2.2 and §3.
- `tests/load/` — the Locust suite; its Hosted UI login is the one
  `smoke_turns.py --auth cognito` ports.
- `backend/scripts/spike_headless_run.py`,
  `backend/scripts/experiment_agent_cache_arms.py` — the other headless-grant
  drivers against dev.
- `docs/TESTING_POSTURE_REPORT.md` — coverage inventory.
