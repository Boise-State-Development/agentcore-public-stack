import { DOCUMENT, isPlatformBrowser } from '@angular/common';
import { HttpClient, HttpContext } from '@angular/common/http';
import { Injectable, PLATFORM_ID, inject } from '@angular/core';
import { SUPPRESS_ERROR_TOAST } from '../../../auth/error.interceptor';
import { ConfigService } from '../../../services/config.service';
import { FEATURES } from '../../../services/features';

/** What the dashboard splits a turn by. Never message content or identity. */
export interface TurnTimingContext {
  turnKind: 'first' | 'followup';
  /** Milliseconds since this tab prewarmed the conversation, or null when it did not. */
  warmAgeMs: number | null;
  modelId?: string | null;
  hasAgent?: boolean;
  hasFiles?: boolean;
}

interface PendingTurn {
  startedAt: number;
  context: TurnTimingContext;
}

/**
 * Measures each turn's time to first token as the user experiences it: from
 * the send to the first streamed delta. Reported once per turn to app-api
 * (`POST /telemetry/turn-timing`), which records it in CloudWatch beside the
 * server-side stages (docs/specs/turn-path-ttft.md §6).
 *
 * The server's own `FirstTokenMs` starts at the Runtime handler, so only this
 * measurement sees the app-api hop, Runtime routing and session start, which
 * is where the V2 Runtime and session prewarm changed the wait.
 *
 * - The report goes out after the first token has arrived, fire-and-forget
 *   with error toasts suppressed, so measuring never delays or disturbs a turn.
 * - A turn that ends before its first token (an error, Stop, a refusal before
 *   any delta) reports nothing.
 * - Only sends start a measurement; a resume or a "Continue" does not.
 * - Off unless `features.clientTurnTiming`.
 */
@Injectable({ providedIn: 'root' })
export class TurnTimingService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly features = inject(FEATURES);
  private readonly document = inject(DOCUMENT);
  private readonly isBrowser = isPlatformBrowser(inject(PLATFORM_ID));

  private readonly pending = new Map<string, PendingTurn>();

  get enabled(): boolean {
    return this.features.clientTurnTiming && this.isBrowser;
  }

  /** The user sent a turn: start its clock. */
  start(sessionId: string, context: TurnTimingContext): void {
    if (!this.enabled) {
      return;
    }
    this.pending.set(sessionId, { startedAt: performance.now(), context });
  }

  /** Details known only once the request is assembled (model, agent, files). */
  describe(sessionId: string, details: Pick<TurnTimingContext, 'modelId' | 'hasAgent' | 'hasFiles'>): void {
    const turn = this.pending.get(sessionId);
    if (turn) {
      turn.context = { ...turn.context, ...details };
    }
  }

  /** A streamed delta arrived. Reports the turn the first time, then forgets it. */
  firstToken(sessionId: string): void {
    const turn = this.pending.get(sessionId);
    if (!turn) {
      return;
    }
    this.pending.delete(sessionId);
    const firstTokenMs = Math.max(0, Math.round(performance.now() - turn.startedAt));
    this.http
      .post(
        `${this.config.appApiUrl()}/telemetry/turn-timing`,
        {
          firstTokenMs,
          turnKind: turn.context.turnKind,
          warmAgeMs: turn.context.warmAgeMs,
          modelId: turn.context.modelId ?? null,
          hasAgent: turn.context.hasAgent ?? false,
          hasFiles: turn.context.hasFiles ?? false,
          visible: this.document.visibilityState === 'visible',
        },
        { context: new HttpContext().set(SUPPRESS_ERROR_TOAST, true) },
      )
      .subscribe({ error: () => undefined });
  }

  /** The stream ended before its first token: there is nothing to report. */
  abandon(sessionId: string): void {
    this.pending.delete(sessionId);
  }
}
