import { HttpClient } from '@angular/common/http';
import { Injectable, InjectionToken, inject } from '@angular/core';
import { EventSourceMessage, fetchEventSource } from '@microsoft/fetch-event-source';
import { firstValueFrom } from 'rxjs';

import { SessionService as BffSessionService } from '../../../auth/session.service';
import { browserFrameHeight } from '../../../services/browser-login/browser-frame-height';
import { ConfigService } from '../../../services/config.service';
import { SessionAffinityService } from '../session/session-affinity.service';
import { BriefFallbackReason } from './handoff.models';

/** How often a preempt re-arms the cancel while the brief's stream is still open. */
const PREEMPT_REARM_MS = 1500;
/** After this, a preempt stops waiting for the brief to close and lets the send go. */
const PREEMPT_MAX_WAIT_MS = 8000;

/** The SSE transport, swappable in specs. */
export const HANDOFF_BRIEF_SSE = new InjectionToken<typeof fetchEventSource>('HANDOFF_BRIEF_SSE', {
  providedIn: 'root',
  factory: () => fetchEventSource,
});

export interface HandoffBriefHandlers {
  /** A piece of the brief, in order. */
  onText(text: string): void;
  /** No brief: the card keeps the excerpt. Called at most once, and never after `onDone`. */
  onFallback(reason: BriefFallbackReason): void;
  /** The brief was written. */
  onDone(result: { briefHash: string; cacheStatus?: string }): void;
}

interface RunningBrief {
  handoffId: string;
  controller: AbortController;
  /** Resolves when the stream has closed, however it closed. */
  closed: Promise<void>;
  preempting?: Promise<void>;
}

/**
 * The handoff brief's own stream (docs/specs/agent-handoffs-and-consults.md §4.5.1).
 *
 * The brief is an invocation on the **source** conversation, but it is not a turn, so it never
 * goes through `ChatHttpService.sendChatRequest`: nothing here touches a session's message map,
 * loading flag, abort controller or turn timing. The source never shows as streaming, so the
 * composer never offers mid-turn steering against a brief.
 *
 * The brief holds the source's single-flight lease while it runs. The user's own send there wins
 * it: `ChatHttpService` calls `preempt` before any turn on a session with a brief in flight, which
 * arms the cancel-only `handoff_brief_preempted` interrupt (never Stop's `user_stopped`, whose
 * marker would put a false interruption note on the source's next turn) and waits for the
 * brief's stream to close, since the Runtime releases the lease before it does.
 *
 * No dependency on the chat services, so `ChatHttpService` can inject it without a cycle.
 */
@Injectable({ providedIn: 'root' })
export class HandoffBriefService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly bffSession = inject(BffSessionService);
  private readonly sessionAffinity = inject(SessionAffinityService);
  private readonly sse = inject(HANDOFF_BRIEF_SSE);

  private readonly running = new Map<string, RunningBrief>();

  /** True while a brief is streaming on this source conversation. */
  isRunning(sourceSessionId: string | null | undefined): boolean {
    return !!sourceSessionId && this.running.has(sourceSessionId);
  }

  /**
   * Stream a brief. `body` is the request a turn on the source would send, plus `handoff_brief`.
   * Resolves when the stream has closed; every outcome reaches `handlers` before then.
   */
  stream(sourceSessionId: string, handoffId: string, body: Record<string, unknown>, handlers: HandoffBriefHandlers): Promise<void> {
    const controller = new AbortController();
    let settle!: () => void;
    const run: RunningBrief = {
      handoffId,
      controller,
      closed: new Promise<void>((resolve) => (settle = resolve)),
    };
    this.running.set(sourceSessionId, run);

    let finished = false;
    const fallback = (reason: BriefFallbackReason) => {
      if (finished) return;
      finished = true;
      handlers.onFallback(reason);
    };
    let httpStatus: number | undefined;

    const baseUrl = this.config.appApiUrl().replace(/\/$/, '');
    const transport = this.sse(`${baseUrl}/chat/stream`, {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        ...this.bffSession.csrfHeaders(),
      },
      body: JSON.stringify(this.withTransportFields(sourceSessionId, body)),
      signal: controller.signal,
      openWhenHidden: true,
      async onopen(response) {
        if (response.ok && response.headers.get('content-type')?.includes('text/event-stream')) return;
        httpStatus = response.status;
        throw new Error(`Brief request failed with status ${response.status}`);
      },
      onmessage: (msg: EventSourceMessage) => {
        if (finished) return;
        const data = parse(msg.data);
        if (msg.event === 'content_block_delta') {
          const text = (data?.['delta'] as { text?: unknown } | undefined)?.text;
          if (typeof text === 'string' && text) handlers.onText(text);
        } else if (msg.event === 'handoff_brief_fallback') {
          fallback(asReason(data?.['reason']));
        } else if (msg.event === 'done') {
          const briefHash = data?.['briefHash'];
          if (typeof briefHash !== 'string' || !briefHash) {
            fallback('error');
            return;
          }
          finished = true;
          const cacheStatus = data?.['cacheStatus'];
          handlers.onDone({ briefHash, cacheStatus: typeof cacheStatus === 'string' ? cacheStatus : undefined });
        }
      },
      onerror: (err) => {
        // Never retry: a second brief is a second model call, and the excerpt is a fine card.
        throw err;
      },
    });

    return transport
      .catch(() => undefined)
      .then(() => {
        // Closed without a verdict: refused before streaming, cut off, or aborted by a preempt.
        if (run.preempting) fallback('preempted');
        else if (httpStatus === 409) fallback('busy');
        else fallback('error');
      })
      .finally(() => {
        if (this.running.get(sourceSessionId) === run) this.running.delete(sourceSessionId);
        settle();
      });
  }

  /**
   * Stop the brief on this source, if there is one, and resolve once its stream has closed, so a
   * send that follows takes the lease instead of answering 409. Idempotent: a second call waits
   * on the same stop. Bounded: past `PREEMPT_MAX_WAIT_MS` the transport is cut and the send goes.
   */
  preempt(sourceSessionId: string): Promise<void> {
    const run = this.running.get(sourceSessionId);
    if (!run) return Promise.resolve();
    run.preempting ??= this.stopAndWait(sourceSessionId, run);
    return run.preempting;
  }

  private async stopAndWait(sourceSessionId: string, run: RunningBrief): Promise<void> {
    const deadline = Date.now() + PREEMPT_MAX_WAIT_MS;
    let closed = false;
    void run.closed.then(() => (closed = true));
    for (;;) {
      // Re-armed until the stream closes: a cancel armed before the brief took the lease finds
      // no owner and is a no-op, so one POST is not enough.
      void this.signalPreempt(sourceSessionId);
      await Promise.race([run.closed, delay(Math.min(PREEMPT_REARM_MS, Math.max(0, deadline - Date.now())))]);
      if (closed) return;
      if (Date.now() >= deadline) {
        run.controller.abort();
        await run.closed;
        return;
      }
    }
  }

  private async signalPreempt(sourceSessionId: string): Promise<void> {
    try {
      await firstValueFrom(
        this.http.post<void>(
          `${this.config.appApiUrl().replace(/\/$/, '')}/sessions/${encodeURIComponent(sourceSessionId)}/interrupt`,
          { reason: 'handoff_brief_preempted' },
        ),
      );
    } catch {
      // Best-effort: the wait above is bounded either way.
    }
  }

  /**
   * The same transport-only fields `ChatHttpService` adds to a turn: the fork pin, so the brief
   * lands on the microVM holding the source's live history, and the sign-in panel's height.
   */
  private withTransportFields(sourceSessionId: string, body: Record<string, unknown>): Record<string, unknown> {
    const out: Record<string, unknown> = { ...body };
    const height = browserFrameHeight();
    if (height !== null) out['browser_frame_height'] = height;
    const affinity = this.sessionAffinity.affinityFor(sourceSessionId);
    if (affinity) out['affinity_session_id'] = affinity;
    return out;
  }
}

const REASONS: ReadonlySet<string> = new Set<BriefFallbackReason>([
  'flag_off',
  'not_open',
  'busy',
  'preempted',
  'quota',
  'refused',
  'timeout',
  'error',
  'tool_use_only',
  'short_source',
  'history_ends_on_user',
]);

function asReason(value: unknown): BriefFallbackReason {
  return typeof value === 'string' && REASONS.has(value) ? (value as BriefFallbackReason) : 'error';
}

function parse(data: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(data);
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
