/**
 * The run sheet's estimate (docs/specs/workflow-runs.md §4, §11): what the steps' first turns cost
 * in input, before any follow-up. Pure, so the sheet and its spec price a run the same way.
 *
 * Each step pays for its own copy of the card, the request and the ticked files, because four
 * Agents have four system prompts and share no cached prefix (§11). The system prompt, tools and
 * output are not counted, so the line says "estimate" and "before any follow-up".
 *
 * A prompt step (workflow-designer §21.3) gets no card, and the files only when it reads them. It
 * is priced as if it paid for them in full: the steps on one model read one cached copy of the
 * documents, so this stays an upper bound, never the bill.
 *
 * A checklist step (§22.3) makes one call per field set, and every checklist call on one model
 * reads one cached copy of the documents (one write per model, then reads, as D.7b measured). So
 * its documents are counted in full once per model (`cacheGroup`), and each further call on that
 * model pays them at the cache-read rate. Each call also pays its own field list and the constant
 * checklist tool and frame (`callExtraTokens`).
 */

/**
 * Tokens a model bills per model-neutral token (`tokens.text`, characters / 4), from the token
 * profile's measured factors: `backend/src/apis/shared/files/tokenizer_costs.py`
 * (`LEGACY_TOKENIZER_FACTOR`, `DEFAULT_TOKENIZER_FACTOR`, `_LEGACY_TOKENIZER_MODELS`), measured on
 * dev 2026-11-10 and summarised in CLAUDE.md's attachment-plan paragraph. Keep the two in step.
 *
 * `GET /files` gives one `estimatedTokens` per document (text, plus page images for a PDF sent
 * natively), so the factor is applied to the whole count. Page images are the same on every
 * model, so this slightly over-states a PDF on the newer tokenizer: the cautious side.
 */
export const TOKENIZER_FACTORS = {
  /** Haiku 4.5 and Sonnet 4.6. */
  legacy: 0.95,
  /** Every other model, and any id not listed. */
  default: 1.4,
} as const;

/** Model ids (by substring) on the legacy tokenizer. */
const LEGACY_TOKENIZER_MODELS = ['claude-haiku-4-5', 'claude-sonnet-4-6'] as const;

export function tokenizerFactor(modelId: string | null | undefined): number {
  const id = (modelId ?? '').toLowerCase();
  return LEGACY_TOKENIZER_MODELS.some((key) => id.includes(key)) ? TOKENIZER_FACTORS.legacy : TOKENIZER_FACTORS.default;
}

/** What a cache read costs against the model's base input rate (Bedrock prompt caching). */
export const CACHE_READ_FACTOR = 0.1;

/** The constant `record_checklist` tool and checklist frame ahead of the documents: ~850 tokens (D.7b). */
export const CHECKLIST_FRAME_TOKENS = 850;

/** One field's line in a checklist call's field list (name, title, type, what to find), roughly. */
export const CHECKLIST_FIELD_TOKENS = 40;

/** What a checklist step adds to its estimate: one call per field set, sharing its model's documents. */
export function checklistEstimate(
  modelId: string | null | undefined,
  fieldSets: readonly { fieldCount?: number }[] | undefined,
): Pick<EstimateStep, 'card' | 'files' | 'callExtraTokens' | 'cacheGroup'> {
  const sets = fieldSets?.length ? fieldSets : [{}];
  return {
    card: false,
    files: true,
    callExtraTokens: sets.map((set) => CHECKLIST_FRAME_TOKENS + (set.fieldCount ?? 0) * CHECKLIST_FIELD_TOKENS),
    cacheGroup: `checklist:${modelId ?? ''}`,
  };
}

/** One step's inputs to the estimate. */
export interface EstimateStep {
  modelId: string | null;
  /** USD per million input tokens; `null` when the catalog gives none. */
  inputPricePerMillionTokens: number | null;
  /** The step's request, which goes in its first message. */
  request: string;
  /** The step is sent the card. False for a prompt step; absent means true. */
  card?: boolean;
  /** The step is sent the ticked files. False for a prompt step that doesn't read them; absent means true. */
  files?: boolean;
  /**
   * One entry per model call the step makes, each the tokens that call adds besides the request
   * and the files: a checklist step's field list and frame. Absent means one call adding nothing.
   */
  callExtraTokens?: readonly number[];
  /**
   * Steps sharing a group read one cached copy of the files: the group's first call pays them in
   * full and every later one at `CACHE_READ_FACTOR`. Absent: every call pays them in full.
   */
  cacheGroup?: string;
}

export interface RunEstimateInput {
  steps: readonly EstimateStep[];
  /** The card as it will be sent ("" when removed). */
  card: string;
  /** Each ticked file's `estimatedTokens`; `null` for a file not yet prepared. */
  fileTokens: readonly (number | null | undefined)[];
}

/** `null` when any step or file can't be priced: the sheet says "estimate unavailable". */
export function estimateRunInputUsd(input: RunEstimateInput): number | null {
  if (input.steps.length === 0) return null;
  const anyFiles = input.steps.some((step) => step.files !== false);
  if (anyFiles && input.fileTokens.some((tokens) => tokens === null || tokens === undefined)) return null;
  const files = input.fileTokens.reduce<number>((sum, tokens) => sum + (tokens ?? 0), 0);
  // The card and request are counted as the server counts its excerpt: four characters a token.
  const card = Math.ceil(input.card.length / 4);
  let total = 0;
  const warmed = new Set<string>();
  for (const step of input.steps) {
    const rate = step.inputPricePerMillionTokens;
    // A rate of 0 is the SPA's placeholder default model, not a free one.
    if (rate === null || !Number.isFinite(rate) || rate <= 0) return null;
    const request = Math.ceil(step.request.length / 4);
    const stepCard = step.card === false ? 0 : card;
    const stepFiles = step.files === false ? 0 : files;
    let sent = 0;
    for (const extra of step.callExtraTokens ?? [0]) {
      const group = step.cacheGroup;
      const cached = group !== undefined && warmed.has(group);
      if (group !== undefined) warmed.add(group);
      sent += stepCard + request + extra + (cached ? stepFiles * CACHE_READ_FACTOR : stepFiles);
    }
    const tokens = sent * tokenizerFactor(step.modelId);
    total += (tokens * rate) / 1_000_000;
  }
  return total;
}

/** "About $0.90" / "Under $0.01": the estimate as the sheet words it. */
export function formatEstimateUsd(usd: number): string {
  if (usd < 0.01) return 'Under $0.01';
  return `About $${usd.toFixed(2)}`;
}
