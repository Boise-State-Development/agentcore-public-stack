import { Injectable, computed, signal } from '@angular/core';
import type { ConsentRequest } from './mcp-app-protocol';

/**
 * Frontend-only consent for App-initiated actions (MCP Apps PR #6).
 *
 * Decision (`docs/kaizen/scoping/mcp-apps-host-renderer.md`, PR #6): unlike
 * the OAuth-tool `oauth_required` SSE family — which exists because OAuth
 * consent fires *inside a backend agent turn* and pauses it via a Strands
 * interrupt — `ui/open-link` and capability requests originate from a
 * postMessage on a possibly-idle iframe. There is no backend turn to
 * pause, so routing through the backend would be pure round-trip
 * ceremony. The host obtains consent entirely on the client: a pending
 * request is surfaced as an inline in-thread prompt (structural sibling of
 * `OAuthConsentService` + the unanchored-prompt strip) and the bridge's
 * JSON-RPC response is held open until the user answers.
 *
 * Transient by design: prompts are interaction gates, not provenance, so
 * they are NOT persisted and do not survive a reload (consistent with the
 * App iframe itself not being re-instantiated on reload).
 */
export interface PendingConsent {
  /** Stable id for `@for` tracking + resolution. */
  id: string;
  request: ConsentRequest;
  receivedAt: number;
  resolve: (granted: boolean) => void;
}

@Injectable({ providedIn: 'root' })
export class McpAppConsentService {
  private readonly pendingSignal = signal<readonly PendingConsent[]>([]);

  /** Pending consent prompts to render in the message-list strip. */
  readonly pending = computed(() => this.pendingSignal());

  private seq = 0;

  /**
   * Surface a consent prompt and resolve once the user answers. The bridge
   * awaits this before opening a link. If the conversation changes while a
   * prompt is open, `reset()` resolves it as denied (fail-closed).
   */
  request(request: ConsentRequest): Promise<boolean> {
    return new Promise<boolean>((resolve) => {
      const id = `mcp-consent-${++this.seq}`;
      const entry: PendingConsent = {
        id,
        request,
        receivedAt: Date.now(),
        resolve,
      };
      this.pendingSignal.set([...this.pendingSignal(), entry]);
    });
  }

  /** User answered a prompt (Allow/Deny). Idempotent. */
  answer(id: string, granted: boolean): void {
    const entry = this.pendingSignal().find((p) => p.id === id);
    if (!entry) return;
    this.pendingSignal.set(this.pendingSignal().filter((p) => p.id !== id));
    entry.resolve(granted);
  }

  /** Drop all prompts on conversation change — fail closed (deny). */
  reset(): void {
    const open = this.pendingSignal();
    if (open.length) {
      this.pendingSignal.set([]);
      for (const e of open) e.resolve(false);
    }
  }
}
