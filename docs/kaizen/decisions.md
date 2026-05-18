# Kaizen Decisions Log

Declined proposals and corrected premises. `kaizen-research` and `kaizen-review-prep`
**must not re-propose** anything here without *materially new context* (a new capability,
a changed upstream constraint, or a new exploit/failure path). Each entry records what
the new context would have to be to re-open it.

---

### [2026-05-18] Declined — Add Reddit `.rss` or Reddit MCP to `kaizen-research`
- **Origin**: review-queue.md (open since 2026-05-10) ▸ research/2026-05-10.md Risks; recommended Decline in reviews/2026-05-15.md ▸ Retirement Candidates.
- **Decision**: Decline.
- **Reasoning**: research/2026-05-15.md confirmed Reddit is blocked at the **domain level** via WebFetch — not just the HTML path. The proposal as scoped (add a Reddit `.rss` source to the research skill) is infeasible with current tooling.
- **Re-open only if**: a Reddit MCP server becomes available, or a `curl`-via-Bash path with a custom User-Agent header is whitelisted. Absent one of those, do not re-surface.

### [2026-05-18] Premise corrected — "Close #266 / #267 as phantom tech debt"
- **Origin**: review-queue.md (open since 2026-05-10) ▸ reviews/2026-05-15.md ▸ Proposal #7 ("close phantom tech debt; features already in our Strands 1.39 pin"). Actioned via PR #338.
- **Decision**: Premise rejected. Issues **#266** (large tool-result offload) and **#267** (context-window lookup fallback) are **not** phantom debt — they are live, well-specified Strands adoption/wiring tasks whose 1.39 precondition is now met. PR #338 posted "unblocked, keep open" comments on both rather than closing them.
- **Reasoning**: The kaizen review assumed the upstream features being present in our pinned Strands version made the issues obsolete. They are not obsolete — they track the *wiring* work to actually adopt those features. Closing them would have silently dropped real, scoped backlog.
- **Re-open only if**: never re-propose *closing* #266/#267 on the "already in our pin" basis. They are valid open work; treat as normal backlog, not kaizen retirement candidates. (Proposing to *implement* them is fine — that is the opposite of this decision.)

### [2026-05-18] Scope note — "Adopt Strands built-in proactive compression, retire our custom `TurnBasedSessionManager` compaction"
- **Origin**: the review-queue Strands-bump entry framed Strands 1.40 proactive compression (PR #2239) as a "library-native subtraction" reducing our custom session-manager compaction surface. Surfaced concretely in PR #340's "Subtraction opportunity (noted, NOT acted on)".
- **Decision**: **Not a drop-in replacement.** Do not propose retiring our custom compaction on a bare "Strands now does this" basis.
- **Reasoning**: Strands' built-in proactive compression operates on `ConversationManager` and only summarizes. Our `TurnBasedSessionManager` compaction additionally does: (1) tool-content truncation, (2) AgentCore-Memory long-term-summary retrieval, (3) DynamoDB-persisted checkpoint state — and drives the PR #243 `compaction` SSE event. The built-in managers do none of (1)–(3).
- **Re-open only if**: a concrete migration design accounts for tool-content truncation, LTM summary retrieval, DynamoDB checkpoint persistence, and the `compaction` SSE-once invariant. A bare "adopt the built-in, delete ours" proposal is out of scope and should not be re-surfaced.
