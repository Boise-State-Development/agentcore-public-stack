import { Injectable, computed, signal } from '@angular/core';
import type { UiResourceEvent } from '../../../shared/utils/stream-parser';

/**
 * Per-conversation registry of MCP App UI resources (SEP-1865), keyed by the
 * originating `toolUseId`. Structural sibling of `ArtifactStateService` /
 * `CompactionSummaryService`: a live SSE path (`recordLive`) and a `reset()`
 * the session page calls on conversation change.
 *
 * The `ui_resource` event is INLINE (it arrives right after its
 * `tool_result`), so a refreshed conversation re-streams nothing. To survive
 * a reload, app-api persists each resource and replays it on the
 * `GET /messages` response's `uiResources` sidecar; `seedFromHydration`
 * re-seeds this registry from that list so the `mcp-app-frame` re-renders.
 * Iframes otherwise persist for the lifetime of the conversation per the
 * scoping doc; teardown is on `reset()`.
 *
 * The whole surface is dark until the backend `AGENTCORE_MCP_APPS_HOST_ENABLED`
 * flag is flipped, so when it's off nothing is recorded or hydrated.
 */
@Injectable({ providedIn: 'root' })
export class McpAppStateService {
  private readonly byToolUseId = signal<ReadonlyMap<string, UiResourceEvent>>(
    new Map(),
  );

  /**
   * Latest streamed partial tool input per `toolUseId` (SEP-1865
   * `tool-input-partial`). Populated while a UI tool's arguments are still
   * streaming, after the frame mounts early; the frame relays each healed
   * prefix to the App for progressive rendering. Last write wins (the backend
   * sends the growing healed prefix). Cleared on `reset()`.
   */
  private readonly partialInputByToolUseId = signal<
    ReadonlyMap<string, Record<string, unknown>>
  >(new Map());

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

  /**
   * Seed resources persisted server-side, replayed on the `GET /messages`
   * `uiResources` sidecar at conversation load. Non-clobbering by
   * `toolUseId` so a slow response can't undo a live `recordLive` entry
   * (matches `ArtifactStateService.seedFromHydration` semantics).
   */
  seedFromHydration(list: readonly UiResourceEvent[]): void {
    if (!list.length) return;
    const next = new Map(this.byToolUseId());
    for (const event of list) {
      if (!next.has(event.toolUseId)) next.set(event.toolUseId, event);
    }
    this.byToolUseId.set(next);
  }

  /**
   * Record the latest streamed partial tool input for a tool invocation
   * (SEP-1865 `tool-input-partial`). Last write wins — the backend streams a
   * growing, server-healed prefix of the arguments object.
   */
  recordPartialInput(
    toolUseId: string,
    args: Record<string, unknown>,
  ): void {
    const next = new Map(this.partialInputByToolUseId());
    next.set(toolUseId, args);
    this.partialInputByToolUseId.set(next);
  }

  /** Latest streamed partial tool input for a tool invocation, or undefined. */
  getPartialInput(toolUseId: string): Record<string, unknown> | undefined {
    return this.partialInputByToolUseId().get(toolUseId);
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
    this.partialInputByToolUseId.set(new Map());
  }
}
