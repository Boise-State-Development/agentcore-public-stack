import { BindingKind } from '../models/agent.model';

/**
 * Agent template client contract (Agent Template Prefill).
 *
 * A template is **data, not code**. The template DATA now lives in the backend catalog
 * (`GET /templates/`, admin-managed) — the "Start from a template" picker fetches it via
 * `AgentTemplatesService`. This module keeps only the shared TYPES and the localStorage
 * key that connects the picker to the create-agent form, so no org-specific template
 * (Course Helper, Q&A, etc.) is baked into the SPA. That is the forkability goal: any org
 * can seed its own templates server-side without touching the client.
 *
 * Each `TemplateDraft` is a deliberately-chosen SUBSET of the `Agent` shape that
 * `getAgent()` returns (see `models/agent.model.ts`), so a template flows through the
 * create-agent form's existing population logic unchanged — the same `patchValue` +
 * binding-decompose path that hydrates an agent for editing.
 *
 * Handoff: the picker writes the selected template's JSON to `localStorage` under
 * `AGENT_TEMPLATE_DRAFT_KEY` and routes to the create-agent form; the form's `ngOnInit`
 * read/reconcile/populate side lives in `agent-form.page.ts`.
 *
 * Contract notes:
 * - `modelConfig.modelId === null` means "platform default" — the form leaves its own
 *   default model selected rather than pinning one.
 * - `bindings[].ref` values are reconciled against the live `/tools/` catalog at
 *   population time: active → apply, deprecated/disabled → apply-but-flag, unknown →
 *   drop-with-notice. Templates therefore name the *intended* capability and do not need
 *   to track catalog drift themselves.
 * - `instructions` is expected to be FULLY WRITTEN prose with guardrails baked in — no
 *   bracket placeholders — so a template is Save-able as-is (after the author adds their
 *   own documents), then edited.
 */

/** The `localStorage` key the picker writes and the form's prefill reader consumes. */
export const AGENT_TEMPLATE_DRAFT_KEY = 'agentTemplateDraft';

/** A model selection on a template. `modelId: null` ⇒ platform default. */
export interface TemplateModelConfig {
  modelId: string | null;
  params?: Record<string, unknown>;
}

/** One binding on a template — the same shape as `AgentBinding`. */
export interface TemplateBinding {
  kind: BindingKind;
  ref: string;
  config?: Record<string, unknown>;
}

/**
 * A curated starting point for a new agent. A subset of the `Agent` shape; the extra
 * `templateId` is a stable catalog handle (never sent to the backend — the form ignores
 * unknown keys) used by the picker and any future "created from template X" analytics.
 */
export interface TemplateDraft {
  templateId: string;
  name: string;
  description: string;
  emoji: string;
  instructions: string;
  tags: string[];
  starters: string[];
  modelConfig: TemplateModelConfig;
  bindings: TemplateBinding[];
}

/**
 * Presentation metadata for the picker. Kept separate from the payload so the card can
 * show a short "what is this for" blurb without polluting the agent `description` that
 * gets written into the form. This is the shape each entry of the public `GET /templates/`
 * response projects (`{ draft, pitch }`).
 */
export interface TemplateCatalogEntry {
  /** The payload written to `localStorage` and fed to the form. */
  draft: TemplateDraft;
  /** One-line pitch shown on the picker card (not part of the agent record). */
  pitch: string;
}
