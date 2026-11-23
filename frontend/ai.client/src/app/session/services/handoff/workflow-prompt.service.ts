import { Injectable, InjectionToken, inject } from '@angular/core';
import { EventSourceMessage, fetchEventSource } from '@microsoft/fetch-event-source';

import { SessionService as BffSessionService } from '../../../auth/session.service';
import { ConfigService } from '../../../services/config.service';
import { SessionAffinityService } from '../session/session-affinity.service';
import { PromptStopReason } from './handoff.models';

/** Prompt steps one invocation may name (`WorkflowPromptRequest`: 1 to 8 ids). */
export const MAX_PROMPT_STEPS_PER_CALL = 8;

/** Why a named step was not started by this call: its row stays as it was (`open`, or another call's). */
export type PromptSkipReason = 'not_in_run' | 'not_prompt' | 'not_open' | 'reads_pending' | 'reads_refused';

/** Why a whole request was refused before any step was claimed. */
export type PromptRefusedReason = 'flag_off' | 'not_found' | 'not_approved' | 'unavailable';

/**
 * One `workflow_prompt_step` frame: a step settled, or was skipped (not started by this call).
 * `kind` says which kind of model step it is (`prompt` or `checklist`, §22.3); the Runtime sends
 * it on every frame but a `not_in_run` skip, and a frame from before it reads as absent.
 */
export type PromptStepFrame = (
  | { stepId: string; status: 'done' }
  | { stepId: string; status: 'stopped'; stoppedReason: PromptStopReason }
  | { stepId: string; status: 'skipped'; reason: PromptSkipReason }
) & { kind?: 'prompt' | 'checklist' };

export interface WorkflowPromptHandlers {
  /** One step's frame, as it settles. Called once per step at most. */
  onStep(frame: PromptStepFrame): void;
  /** The whole request was refused before any claim; no `onStep` follows. */
  onRefused(reason: PromptRefusedReason): void;
}

/** The SSE transport, swappable in specs. */
export const WORKFLOW_PROMPT_SSE = new InjectionToken<typeof fetchEventSource>('WORKFLOW_PROMPT_SSE', {
  providedIn: 'root',
  factory: () => fetchEventSource,
});

/**
 * The body of a `workflow_prompt` invocation (docs/specs/workflow-designer.md §21.3): the run's
 * **source** session, so it lands on the source's Runtime session, and the steps to run. Nothing
 * else: the Runtime builds no agent, restores no history and offers no tools for it, so none of a
 * turn's fields (model, tools, system prompt) would be read. `session_id` is the one field
 * `InvocationRequest` requires; `message` is left at its default.
 */
export function workflowPromptBody(sourceSessionId: string, runId: string, stepIds: readonly string[]): Record<string, unknown> {
  return {
    session_id: sourceSessionId,
    workflow_prompt: { runId, stepIds: [...new Set(stepIds)].slice(0, MAX_PROMPT_STEPS_PER_CALL) },
  };
}

/**
 * A workflow run's prompt and checklist steps, sent on their own invocation
 * (docs/specs/workflow-designer.md §21.3, PR-D.6c; §22.3, PR-D.7c: a checklist step rides the
 * same invocation and the same `stepIds`, and its frame says `kind: checklist`).
 *
 * Like the handoff brief, this is an invocation on the source conversation that is not a turn, so
 * it never goes through `ChatHttpService`: no message map, loading flag, abort controller or turn
 * timing is touched, and the source never shows as streaming. Unlike the brief it takes no session
 * lease, so the user keeps chatting in the source and nothing here needs preempting.
 *
 * No text is streamed: each step's frame says how it settled, and its answer is read from its row.
 * A closed tab does not cancel the calls (they finish and write their rows); Stop run does, through
 * the run row the Runtime watches.
 */
@Injectable({ providedIn: 'root' })
export class WorkflowPromptService {
  private readonly config = inject(ConfigService);
  private readonly bffSession = inject(BffSessionService);
  private readonly sessionAffinity = inject(SessionAffinityService);
  private readonly sse = inject(WORKFLOW_PROMPT_SSE);

  /**
   * Run these prompt steps of an approved run. Resolves once the stream has closed, however it
   * closed (never rejects); every frame reached `handlers` before then. A step that got no frame
   * may still be running on the Runtime: the caller re-reads its row.
   */
  async run(sourceSessionId: string, runId: string, stepIds: readonly string[], handlers: WorkflowPromptHandlers): Promise<void> {
    const seen = new Set<string>();
    let refused = false;
    const body = workflowPromptBody(sourceSessionId, runId, stepIds);
    const affinity = this.sessionAffinity.affinityFor(sourceSessionId);
    if (affinity) body['affinity_session_id'] = affinity;
    const baseUrl = this.config.appApiUrl().replace(/\/$/, '');
    try {
      await this.sse(`${baseUrl}/chat/stream`, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
          ...this.bffSession.csrfHeaders(),
        },
        body: JSON.stringify(body),
        openWhenHidden: true,
        async onopen(response) {
          if (response.ok && response.headers.get('content-type')?.includes('text/event-stream')) return;
          throw new Error(`Prompt steps request failed with status ${response.status}`);
        },
        onmessage: (msg: EventSourceMessage) => {
          if (refused) return;
          const data = parse(msg.data);
          if (!data || data['runId'] !== runId) return;
          if (msg.event === 'workflow_prompt_refused') {
            refused = true;
            handlers.onRefused(asRefused(data['reason']));
            return;
          }
          if (msg.event !== 'workflow_prompt_step') return;
          const frame = asFrame(data);
          if (!frame || seen.has(frame.stepId)) return;
          seen.add(frame.stepId);
          handlers.onStep(frame);
        },
        onerror: (err) => {
          // Never retry: a resend would ask for the same steps, which their claims already hold.
          throw err;
        },
      });
    } catch {
      // Closed without `done`: the caller re-reads the rows of the steps that got no frame.
    }
  }
}

const STOP_REASONS: ReadonlySet<string> = new Set<PromptStopReason>(['user_stopped', 'too_large', 'model_refused', 'quota', 'timeout', 'error']);
const SKIP_REASONS: ReadonlySet<string> = new Set<PromptSkipReason>(['not_in_run', 'not_prompt', 'not_open', 'reads_pending', 'reads_refused']);
const REFUSED_REASONS: ReadonlySet<string> = new Set<PromptRefusedReason>(['flag_off', 'not_found', 'not_approved', 'unavailable']);

function asFrame(data: Record<string, unknown>): PromptStepFrame | null {
  const stepId = data['stepId'];
  if (typeof stepId !== 'string' || !stepId) return null;
  const raw = data['kind'];
  const kind: { kind?: 'prompt' | 'checklist' } = raw === 'prompt' || raw === 'checklist' ? { kind: raw } : {};
  switch (data['status']) {
    case 'done':
      return { stepId, status: 'done', ...kind };
    case 'stopped': {
      const reason = data['stoppedReason'];
      return {
        stepId,
        status: 'stopped',
        stoppedReason: typeof reason === 'string' && STOP_REASONS.has(reason) ? (reason as PromptStopReason) : 'error',
        ...kind,
      };
    }
    case 'skipped': {
      const reason = data['reason'];
      // An unknown skip reads as `not_open`: the row is re-read, never resent blind.
      return {
        stepId,
        status: 'skipped',
        reason: typeof reason === 'string' && SKIP_REASONS.has(reason) ? (reason as PromptSkipReason) : 'not_open',
        ...kind,
      };
    }
    default:
      return null;
  }
}

function asRefused(value: unknown): PromptRefusedReason {
  return typeof value === 'string' && REFUSED_REASONS.has(value) ? (value as PromptRefusedReason) : 'unavailable';
}

function parse(data: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(data);
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}
