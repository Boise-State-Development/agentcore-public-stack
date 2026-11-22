/**
 * The models a prompt step can run on (docs/specs/workflow-designer.md §21.3), as the designer's
 * model picker lists them.
 *
 * The list starts from `ModelService.availableModels`: the catalog's enabled rows that `/models`
 * already filtered to the ones this user can use. On top of that, the rule mirrors
 * `apis/shared/handoff/prompt_models.py` (`prompt_model_unsupported_reason`), so the picker offers
 * what the run sheet and the Runtime accept. Keep the two in step:
 *
 * - **Bedrock Converse only** (provider `bedrock`): a prompt step is one Converse call.
 * - **Text in, text alone out**: a speech or an embedding model writes no text answer.
 * - **A step that reads the run's files** needs a model that takes more than text, since a
 *   TEXT-only model refuses a document block. Such a model can still run a step that reads only
 *   earlier steps' results, so it is listed, and shown unavailable while the step reads files.
 *
 * A model being retired is left out, as the chat picker leaves it out: its successor is the one to
 * choose. The server still judges the saved model as the starter (`ModelView.canRun`).
 */

import { ManagedModel } from '../admin/manage-models/models/managed-model.model';
import { isRetiring } from '../shared/utils/retirement';

/** The catalog provider that speaks Bedrock Converse (`PROMPT_STEP_PROVIDER`). */
export const PROMPT_STEP_PROVIDER = 'bedrock';

export interface PromptModelChoice {
  modelId: string;
  name: string;
  /** False for a TEXT-only model: it can't be sent the run's documents. */
  readsDocuments: boolean;
}

export function promptModelChoices(models: readonly ManagedModel[]): PromptModelChoice[] {
  return models
    .filter((model) => !isRetiring(model) && promptModelSupported(model))
    .map((model) => ({
      modelId: model.modelId,
      name: model.modelName || model.modelId,
      readsDocuments: !isTextOnly(model.inputModalities),
    }));
}

/** Whether a catalog row can run some prompt step (one that reads files or one that doesn't). */
export function promptModelSupported(model: Pick<ManagedModel, 'provider' | 'inputModalities' | 'outputModalities'>): boolean {
  if (String(model.provider ?? '').trim().toLowerCase() !== PROMPT_STEP_PROVIDER) return false;
  const inputs = upper(model.inputModalities);
  const outputs = upper(model.outputModalities);
  return inputs.has('TEXT') && outputs.size === 1 && outputs.has('TEXT');
}

function isTextOnly(modalities: readonly string[] | undefined): boolean {
  const inputs = upper(modalities);
  return inputs.size === 1 && inputs.has('TEXT');
}

function upper(values: readonly string[] | undefined): Set<string> {
  return new Set((values ?? []).map((value) => String(value).trim().toUpperCase()).filter(Boolean));
}
