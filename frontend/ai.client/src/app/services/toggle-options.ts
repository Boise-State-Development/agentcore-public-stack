/**
 * Options shared by the preference-writing toggles on `ToolService` and
 * `SkillService`.
 *
 * `respectAgentLock` exists because both services are root singletons while the
 * Agent binding lock is conversation-scoped state: the session view sets it via
 * `lockToAgentTools` / `lockToAgentSkills` and does NOT release it on teardown
 * (`session.page.ts` clears it only when it later loads an unbound
 * conversation). A user who leaves an agent-bound chat therefore carries the
 * lock with them.
 *
 * Conversation surfaces — the composer settings drawer — leave this `true`, so
 * locked rows stay honestly inert. Global surfaces — Customize — pass `false`:
 * a user's own preference page must not be silently read-only because of
 * whichever conversation they happened to visit last.
 *
 * See `docs/specs/customize-surface.md` §"The agent-lock seam".
 */
export interface ToggleOptions {
  respectAgentLock?: boolean;
}
