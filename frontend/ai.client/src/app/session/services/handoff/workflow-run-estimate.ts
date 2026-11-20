/**
 * The run sheet's estimate (docs/specs/workflow-runs.md §4, §11): what the steps' first turns cost
 * in input, before any follow-up. Pure, so the sheet and its spec price a run the same way.
 *
 * Each step pays for its own copy of the card, the request and the ticked files, because four
 * Agents have four system prompts and share no cached prefix (§11). The system prompt, tools and
 * output are not counted, so the line says "estimate" and "before any follow-up".
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

/** One step's inputs to the estimate. */
export interface EstimateStep {
  modelId: string | null;
  /** USD per million input tokens; `null` when the catalog gives none. */
  inputPricePerMillionTokens: number | null;
  /** The step's request, which goes in its first message. */
  request: string;
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
  if (input.fileTokens.some((tokens) => tokens === null || tokens === undefined)) return null;
  const files = input.fileTokens.reduce<number>((sum, tokens) => sum + (tokens ?? 0), 0);
  // The card and request are counted as the server counts its excerpt: four characters a token.
  const card = Math.ceil(input.card.length / 4);
  let total = 0;
  for (const step of input.steps) {
    const rate = step.inputPricePerMillionTokens;
    // A rate of 0 is the SPA's placeholder default model, not a free one.
    if (rate === null || !Number.isFinite(rate) || rate <= 0) return null;
    const tokens = (card + Math.ceil(step.request.length / 4) + files) * tokenizerFactor(step.modelId);
    total += (tokens * rate) / 1_000_000;
  }
  return total;
}

/** "About $0.90" / "Under $0.01": the estimate as the sheet words it. */
export function formatEstimateUsd(usd: number): string {
  if (usd < 0.01) return 'Under $0.01';
  return `About $${usd.toFixed(2)}`;
}
