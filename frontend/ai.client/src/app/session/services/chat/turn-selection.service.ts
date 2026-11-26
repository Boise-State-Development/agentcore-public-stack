import { inject, Injectable } from '@angular/core';
import { IncognitoChatService } from '../incognito';
import { ModelService } from '../model/model.service';
import { ToolService } from '../../../services/tool/tool.service';
import { SkillService } from '../../../services/skill/skill.service';
import { SystemPromptsService } from '../../../services/system-prompts/system-prompts.service';

/** How long to wait for the tool and skill lists before using whatever has loaded. */
const SELECTION_SOURCE_TIMEOUT_MS = 4000;

/** What a turn's selection depends on beyond the services' own state. */
export interface TurnSelectionContext {
  /** The Agent (or project harness) the conversation is bound to. */
  assistantId?: string;
  /** An `@`-mentioned Agent that wins for this one turn. */
  mentionAgentId?: string;
  /** Skills invoked with a `/` command; a subset of the enabled ones. */
  invokedSkillIds?: string[];
  /**
   * Fields the send places between the model/skill keys and the Agent keys
   * (its attachments), so the posted object keeps its key order exactly.
   */
  attachmentFields?: Record<string, unknown>;
}

/**
 * The selection fields of a chat turn's request body: model, tools, skills,
 * Agent, mode. ONE builder, used by the send (`ChatRequestService`) and by
 * session prewarm's prepare (`SessionPrewarmService`), so the agent the backend
 * builds ahead of the send is the one the send will ask for. A second copy
 * would drift, and a drifted prepare builds an agent nobody asks for.
 */
@Injectable({ providedIn: 'root' })
export class TurnSelectionService {
  private readonly modelService = inject(ModelService);
  private readonly toolService = inject(ToolService);
  private readonly skillService = inject(SkillService);
  private readonly incognitoChat = inject(IncognitoChatService);
  private readonly systemPromptsService = inject(SystemPromptsService);

  /**
   * Wait for the tool and skill selections to be loaded before a request is
   * assembled from them.
   *
   * WHY: both lists arrive asynchronously, and until they land
   * `getEnabledToolIds()` / `getEnabledSkillIds()` answer with an empty array,
   * which is indistinguishable from "the user turned everything off". A turn
   * built from that discloses no skills and a short tool list, and the next
   * turn discloses the real ones: the whole cacheable prefix is rewritten at
   * the cache-write premium for nothing the user did.
   *
   * The wait is bounded and never fails the send: a slow or broken response
   * falls back to assembling from whatever is loaded. Once loaded this costs
   * nothing.
   */
  async awaitSources(): Promise<void> {
    const settled = Promise.all([
      this.toolService.ensureLoaded(),
      this.skillService.ensureLoaded(),
    ]);

    let timer: ReturnType<typeof setTimeout> | undefined;
    const bound = new Promise<void>(resolve => {
      timer = setTimeout(resolve, SELECTION_SOURCE_TIMEOUT_MS);
    });

    try {
      await Promise.race([settled.then(() => undefined), bound]);
    } catch {
      // `ensureLoaded` is documented not to reject; a send must not fail here
      // even if that ever changes.
    } finally {
      if (timer !== undefined) {
        clearTimeout(timer);
      }
    }
  }

  /** Whether both lists the selection reads have loaded. */
  sourcesLoaded(): boolean {
    return this.toolService.initialized() && this.skillService.initialized();
  }

  /** Whether the new-chat composer's incognito toggle is on, unclaimed by any conversation yet. */
  incognitoPending(): boolean {
    return this.incognitoChat.pending();
  }

  /**
   * The turn's selection. Throws when no model is selected.
   */
  build(sessionId: string, ctx: TurnSelectionContext = {}): Record<string, unknown> {
    const selectedModel = this.modelService.getSelectedModel();

    if (!selectedModel) {
      throw new Error('No model selected. Please select a model before sending a message.');
    }

    // If using the system default model, send null for model_id to let backend use its default
    const isDefaultModel = this.modelService.isUsingDefaultModel();

    const selection: Record<string, unknown> = {
      model_id: isDefaultModel ? null : selectedModel.modelId,
      enabled_tools: this.toolService.getEnabledToolIds(),
      provider: isDefaultModel ? null : selectedModel.provider,
    };

    // Incognito chat (#1738). The backend honours it only on the turn that
    // creates the session; sending it on later turns (e.g. a retry) is harmless.
    if (this.incognitoChat.isIncognito(sessionId)) {
      selection['incognito'] = true;
    }

    // Skills v2 D6: skills are opt-in, so send the selection only when there is
    // one. Omitting the key is the "no skills" signal the backend already
    // defaults to, which keeps a plain turn's payload identical to before and
    // avoids paying for the skill resolution server-side. Agent-bound
    // conversations send the locked set, but the backend re-resolves an Agent's
    // bindings per invoker and replaces this outright — the client is never the
    // authority here.
    const enabledSkillIds = this.skillService.getEnabledSkillIds();
    if (enabledSkillIds.length > 0) {
      selection['enabled_skills'] = enabledSkillIds;
    }

    // Skills the user invoked with a `/` command in the composer. A strict subset of
    // `enabled_skills` — the menu only offers skills that are already on — so this
    // changes nothing about what the turn discloses and nothing about the cacheable
    // prefix. The backend intersects it against the turn's effective set anyway (an
    // Agent's bindings can still replace that set) and appends a one-line directive to
    // the user message telling the model to activate the named skill.
    if (ctx.invokedSkillIds && ctx.invokedSkillIds.length > 0) {
      selection['invoked_skills'] = ctx.invokedSkillIds;
    }

    // Per-model inference param overrides, set either in the Settings →
    // Advanced panel or from the model picker's Effort submenu (which writes
    // the `effort` / `reasoning_effort` param through the same store).
    // Backend layers these on top of admin defaults and clamps to the model's
    // bounds; locked params drop the override silently.
    if (!isDefaultModel) {
      const overrides = this.modelService.getInferenceParamOverrides();
      if (Object.keys(overrides).length > 0) {
        selection['inference_params'] = overrides;
      }
    }

    if (ctx.attachmentFields) {
      Object.assign(selection, ctx.attachmentFields);
    }

    // Add assistant ID if present
    // NOTE: Field name is 'rag_assistant_id' to avoid collision with AWS Bedrock
    // AgentCore Runtime's internal 'assistant_id' field handling (causes 424 error)
    //
    // Marketplace D11: an `@`-mention wins for this one turn and rides the SAME field,
    // with `agent_mention` telling the backend not to treat it as a binding — it skips
    // the "one assistant per session" validation and does not write session preferences.
    // Sending it as a *different* field would have meant teaching every downstream step
    // (RAG, binding resolution, memory injection, the resume snapshot) about a second
    // way to name the agent running the turn; the flag keeps one.
    //
    // A mention beats the bound assistant deliberately: the user just named who they
    // want, in the composer, for this message.
    if (ctx.mentionAgentId) {
      selection['rag_assistant_id'] = ctx.mentionAgentId;
      selection['agent_mention'] = true;
    } else if (ctx.assistantId) {
      selection['rag_assistant_id'] = ctx.assistantId;
    } else {
      // Forward the active conversation mode for non-assistant turns. The
      // assistant path is intentionally excluded server-side too — assistants
      // are KB-grounded and a "mode" prompt could contradict the assistant's
      // own instructions. Sending the id every turn lets the inference path
      // resolve the prompt without round-tripping session metadata, which
      // matters on the first turn of a brand-new session (no metadata row
      // exists yet).
      const activePromptId = this.systemPromptsService.activePromptId();
      if (activePromptId) {
        selection['selected_prompt_id'] = activePromptId;
      }
    }

    return selection;
  }
}
