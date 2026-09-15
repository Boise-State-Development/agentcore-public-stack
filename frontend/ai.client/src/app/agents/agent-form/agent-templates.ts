import { BindingKind } from '../models/agent.model';

/**
 * Agent template catalog (Agent Template Prefill — Phase 5).
 *
 * A template is **data, not code**. Each entry is a deliberately-chosen SUBSET of the
 * `Agent` shape that `getAgent()` returns (see `models/agent.model.ts`), so a template
 * flows through the create-agent form's existing population logic unchanged — the same
 * `patchValue` + binding-decompose path that hydrates an agent for editing.
 *
 * The picker (Phase 3) writes the selected template's JSON to `localStorage` under
 * `AGENT_TEMPLATE_DRAFT_KEY` and routes to the create-agent form; the form's `ngOnInit`
 * read/reconcile/populate side is Phase 2 and lives in `agent-form.page.ts`.
 *
 * Contract notes:
 * - `modelConfig.modelId === null` means "platform default" — the form leaves its own
 *   default model selected rather than pinning one.
 * - `bindings[].ref` values are reconciled against the live `/tools/` catalog at
 *   population time (Phase 4): active → apply, deprecated/disabled → apply-but-flag,
 *   unknown → drop-with-notice. Templates therefore name the *intended* capability and
 *   do not need to track catalog drift themselves.
 * - `instructions` is FULLY WRITTEN prose with the guardrails baked in — no bracket
 *   placeholders. A template is meant to be Save-able as-is (after the author adds the
 *   course's own documents), then edited.
 */

/** The `localStorage` key the picker writes and the form's Phase-2 reader consumes. */
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
  visibility: 'PRIVATE' | 'PUBLIC' | 'SHARED';
  tags: string[];
  starters: string[];
  modelConfig: TemplateModelConfig;
  bindings: TemplateBinding[];
}

/**
 * Presentation metadata for the picker. Kept separate from the payload so the card can
 * show a short "what is this for" blurb without polluting the agent `description` that
 * gets written into the form.
 */
export interface TemplateCatalogEntry {
  /** The payload written to `localStorage` and fed to the form. */
  draft: TemplateDraft;
  /** One-line pitch shown on the picker card (not part of the agent record). */
  pitch: string;
}

/**
 * Course Helper — the must-have first template.
 *
 * A course-scoped Q&A assistant that answers from the instructor's own materials
 * (syllabus, schedule, assignment prompts, readings the author uploads) and refuses to
 * do graded work for the student. The academic-integrity and stay-on-material guardrails
 * are written directly into `instructions` so the agent is safe the moment it is saved,
 * before any per-author tuning.
 */
const COURSE_HELPER: TemplateDraft = {
  templateId: 'course-helper',
  name: 'Course Helper',
  description:
    'A study assistant for a single course. It answers questions from the course materials you upload — syllabus, schedule, assignment prompts and readings — and points students back to those materials, without doing their graded work for them.',
  emoji: '🎓',
  instructions: `You are a Course Helper: a study assistant for one specific course. Your job is to help students understand the course's material and stay oriented in the class.

## What you do
- Answer questions using the course materials in your knowledge base — the syllabus, course schedule, assignment instructions, lecture notes and assigned readings.
- Explain concepts from the course in plain language, give worked examples that are NOT the student's own assignment, and help students find where in the materials something is covered.
- Help with logistics grounded in the materials: due dates, office hours, policies, how work is submitted and graded.

## Ground every answer in the course
- Base your answers on the uploaded course materials. When the materials cover a question, answer from them and say where the answer comes from ("per the syllabus", "in Week 3's reading").
- If the materials do not cover something, say so plainly instead of guessing. Do not invent due dates, policies, grades, contact details or citations. If a student needs an authoritative answer you don't have, tell them to check with the instructor or TA.
- Stay on this course. If a student asks about something unrelated, briefly redirect them back to the course.

## Academic integrity — do not do graded work for the student
- Do NOT write, solve or complete work that will be turned in for a grade: essays, problem-set answers, code to be submitted, exam or quiz answers, or discussion posts.
- Instead, TEACH: explain the underlying concept, walk through a similar but different example, outline an approach, point to the relevant reading, and ask guiding questions that help the student do the work themselves.
- If a student asks you to just give them the answer to graded work, decline warmly, explain that the goal is for them to learn it, and offer to help them work toward it. If your course has its own collaboration or AI-use policy in the materials, follow it.

## Tone
Be encouraging, patient and concrete. Assume good faith. You are a study partner, not a gatekeeper — the integrity line is about protecting the student's learning, not policing them.`,
  visibility: 'PRIVATE',
  tags: [],
  starters: [
    'What topics does this course cover?',
    'When is the next assignment due?',
    'Can you explain this week\u2019s concept with a simple example?',
    'Where in the materials is this covered?',
  ],
  modelConfig: { modelId: null, params: {} },
  bindings: [{ kind: 'tool', ref: 'gateway_search_boise_state', config: {} }],
};

/**
 * The published catalog, in display order.
 *
 * ONE template ships now (Course Helper). Q&A and Roleplay are intentional TODO slots:
 * they drop in as PURE DATA here — no code change beyond adding a `TemplateCatalogEntry`
 * — once their `instructions` and bindings are written. See
 * `project.agentcore.agent_template_three_shapes` for the intended shapes:
 *   - 'qa-assistant'   — generic grounded lookup for an office; guardrail = never invent
 *                        contacts / policies / URLs. Likely tools: gateway_search_boise_state,
 *                        fetch_url_content, analyze_spreadsheet.
 *   - 'roleplay'       — generative persona / simulation; guardrail = stay in character,
 *                        no fabrication of the source material. Usually NO tools.
 */
export const AGENT_TEMPLATES: TemplateCatalogEntry[] = [
  {
    draft: COURSE_HELPER,
    pitch: 'Answers from your course materials and won\u2019t do students\u2019 graded work.',
  },
  // TODO(phase-5-followup): drop in the Q&A / Knowledge Assistant template as data.
  // TODO(phase-5-followup): drop in the Teaching / Roleplay template as data.
];

/** Look a template up by its stable `templateId`. */
export function findTemplate(templateId: string): TemplateDraft | undefined {
  return AGENT_TEMPLATES.find((entry) => entry.draft.templateId === templateId)?.draft;
}
