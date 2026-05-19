import { Injectable, computed, signal } from '@angular/core';
import type { UiResourceEvent } from '../../../shared/utils/stream-parser';

/**
 * Per-conversation registry of MCP App UI resources (SEP-1865), keyed by the
 * originating `toolUseId`. Structural sibling of `ArtifactStateService` /
 * `CompactionSummaryService`: a live SSE path (`recordLive`) and a `reset()`
 * the session page calls on conversation change.
 *
 * The `ui_resource` event is INLINE (it arrives right after its
 * `tool_result`), so unlike artifacts there is no session-load hydration
 * path — a refreshed conversation re-streams nothing, and a persisted MCP
 * App is out of scope until a later PR. Iframes persist for the lifetime of
 * the conversation per the scoping doc; teardown is on `reset()`.
 *
 * The whole surface is dark until the backend `AGENTCORE_MCP_APPS_HOST_ENABLED`
 * flag is flipped (PR #7), so in practice nothing is recorded here yet.
 */
@Injectable({ providedIn: 'root' })
export class McpAppStateService {
  private readonly byToolUseId = signal<ReadonlyMap<string, UiResourceEvent>>(
    new Map(),
  );

  /** True once any MCP App resource has been recorded this conversation. */
  readonly hasApps = computed(() => this.byToolUseId().size > 0);

  /**
   * Record the UI resource for a tool invocation. Last write wins — a tool
   * that re-emits for the same `toolUseId` replaces the prior resource
   * (the iframe rebinds to the new HTML). New invocations get new ids.
   */
  recordLive(event: UiResourceEvent): void {
    const next = new Map(this.byToolUseId());
    next.set(event.toolUseId, event);
    this.byToolUseId.set(next);
  }

  /** The UI resource for a tool invocation, or undefined. */
  get(toolUseId: string): UiResourceEvent | undefined {
    return this.byToolUseId().get(toolUseId);
  }

  /**
   * Whether this tool invocation has an MCP App. Reads the backing signal,
   * so a `computed()` that calls it stays reactive to the `ui_resource`
   * event arriving after the tool-use block first renders.
   */
  has(toolUseId: string): boolean {
    return this.byToolUseId().has(toolUseId);
  }

  /** Drop all resources — called on conversation change (teardown). */
  reset(): void {
    this.byToolUseId.set(new Map());
  }
}
