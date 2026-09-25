# AgentCore Memory audit

Evidence for the Phase 0 decision in `docs/specs/shared-projects.md` §1 and
`docs/specs/memory-baseline-decision.md`: is AgentCore long-term memory working
in a deployed environment, and what does it cost?

## Requirements

- boto3 >= 1.43 (for `bedrock-agentcore` and `bedrock-agentcore-control`). The
  backend's `agentcore` extra has it; older system AWS CLIs do not.
- Credentials for the target account via `--profile` or `AWS_PROFILE`.

## Commands

```bash
# Read-only: strategies + namespace comparison, actors, record census,
# extraction jobs, runtime log signals (FilterLogEvents, 7-day window)
python scripts/memory-audit/audit.py --profile dev-ai --region us-west-2 \
  --prefix dev-boisestateai-v2 --out /tmp/memaudit inventory

# Writes (dev only): a synthetic actor states a fact, the script waits for
# extraction, replays retrieval with the runtime's parameters, then deletes
# the records and events it created
python scripts/memory-audit/audit.py --profile dev-ai --region us-west-2 \
  --prefix dev-boisestateai-v2 --out /tmp/memaudit probe

# Late-arriving probe records (the probe prints the actor to pass)
python scripts/memory-audit/audit.py ... cleanup --cleanup-actor memory-audit-probe-<uuid>
```

`inventory` walks every actor's sessions, so it takes a few minutes on a
deployment with ~100 actors.

## Output and privacy

`--out` gets two things:

- `summary.json`: aggregates only (counts, histograms, namespace shapes with
  ids replaced by placeholders, retrieval scores). This is what goes into
  docs and PRs.
- `raw/`: actor ids, record text, log lines. **This is personal data. Keep it
  out of the repository** (the repo is public). Use a scratch directory.

## App-level behavioral test (manual)

The probe tests the service. The spec also wants the app path tested
(§1.2 step 5):

1. As a test user, start chat A and state a clearly synthetic fact.
2. Wait ~90 s (extraction took ~70 s in dev).
3. Start chat B and ask for the fact. Pass = it's recalled and the runtime log
   shows `Retrieved N customer context items` for that turn.
4. Clean up: delete both chats, then delete the extracted records. Deleting a
   chat does **not** delete them (spec §1.1 gap a).
