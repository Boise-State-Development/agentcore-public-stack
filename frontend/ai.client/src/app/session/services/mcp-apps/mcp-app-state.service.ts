import { Injectable, signal } from '@angular/core';
import type { UiResourceEvent } from '../../../shared/utils/stream-parser';

/**
 * Registry of MCP App UI resources (SEP-1865), keyed by conversation and then
 * by the originating `toolUseId`.
 *
 * Unlike `ArtifactStateService` / `CompactionSummaryService` — which are
 * viewed-session-scoped and reset on conversation change — this registry
 * RETAINS every conversation it has seen for the lifetime of the SPA session,
 * mirroring the message cache in `MessageMapService`. That retention is the
 * whole point: the `ui_resource` event is inline (it arrives at the tool's
 * `content_block_start`, mid-stream) and never re-streams, and the only
 * server-side replay is the `uiResources` sidecar on `GET /messages` — a
 * request `loadMessagesForSession` deliberately skips when the conversation's
 * messages are already in memory. A registry that reset on navigation
 * therefore had no way back: leaving a conversation and returning to it
 * dropped every App frame to a plain tool card until a hard refresh.
 *
 * Reads are scoped to a conversation so a `toolUseId` can only ever resolve
 * inside the conversation that produced it. Writes carry their own session id
 * (the streaming session, which is not necessarily the viewed one — a
 * background conversation's Apps are recorded too, and are there when the user
 * navigates back).
 *
 * Iframe teardown is not this registry's job: a frame unmounts when its
 * message-list component is destroyed on conversation change.
 *
 * The whole surface is dark until the backend `AGENTCORE_MCP_APPS_HOST_ENABLED`
 * flag is flipped, so when it's off nothing is recorded or hydrated.
 */
@Injectable({ providedIn: 'root' })
export class McpAppStateService {
  private readonly bySession = signal<
    ReadonlyMap<string, ReadonlyMap<string, UiResourceEvent>>
  >(new Map());

  /**
   * Latest streamed partial tool input per conversation and `toolUseId`
   * (SEP-1865 `tool-input-partial`). Populated while a UI tool's arguments are
   * still streaming, after the frame mounts early; the frame relays each
   * healed prefix to the App for progressive rendering. Last write wins (the
   * backend sends the growing healed prefix). Retained alongside the
   * resources.
   */
  private readonly partialInputBySession = signal<
    ReadonlyMap<string, ReadonlyMap<string, Record<string, unknown>>>
  >(new Map());

  /**
   * Record the UI resource for a tool invocation. Last write wins — a tool
   * that re-emits for the same `toolUseId` replaces the prior resource
   * (the iframe rebinds to the new HTML). New invocations get new ids.
   */
  recordLive(sessionId: string, event: UiResourceEvent): void {
    this.bySession.update(map =>
      writeEntry(map, sessionId, event.toolUseId, event),
    );
  }

  /**
   * Seed resources persisted server-side, replayed on the `GET /messages`
   * `uiResources` sidecar at conversation load. Non-clobbering by
   * `toolUseId` so a slow response can't undo a live `recordLive` entry
   * (matches `ArtifactStateService.seedFromHydration` semantics).
   */
  seedFromHydration(
    sessionId: string,
    list: readonly UiResourceEvent[],
  ): void {
    if (!list.length) return;
    this.bySession.update(map => {
      const existing = map.get(sessionId);
      const next = new Map(existing ?? []);
      for (const event of list) {
        if (!next.has(event.toolUseId)) next.set(event.toolUseId, event);
      }
      if (existing && next.size === existing.size) return map;
      return new Map(map).set(sessionId, next);
    });
  }

  /**
   * Record the latest streamed partial tool input for a tool invocation
   * (SEP-1865 `tool-input-partial`). Last write wins — the backend streams a
   * growing, server-healed prefix of the arguments object.
   */
  recordPartialInput(
    sessionId: string,
    toolUseId: string,
    args: Record<string, unknown>,
  ): void {
    this.partialInputBySession.update(map =>
      writeEntry(map, sessionId, toolUseId, args),
    );
  }

  /** Latest streamed partial tool input for a tool invocation, or undefined. */
  getPartialInput(
    sessionId: string | null,
    toolUseId: string,
  ): Record<string, unknown> | undefined {
    if (!sessionId) return undefined;
    return this.partialInputBySession().get(sessionId)?.get(toolUseId);
  }

  /** The UI resource for a tool invocation, or undefined. */
  get(
    sessionId: string | null,
    toolUseId: string,
  ): UiResourceEvent | undefined {
    if (!sessionId) return undefined;
    return this.bySession().get(sessionId)?.get(toolUseId);
  }

  /**
   * Whether this tool invocation has an MCP App. Reads the backing signal,
   * so a `computed()` that calls it stays reactive to the `ui_resource`
   * event arriving after the tool-use block first renders.
   */
  has(sessionId: string | null, toolUseId: string): boolean {
    if (!sessionId) return false;
    return this.bySession().get(sessionId)?.has(toolUseId) ?? false;
  }

  /** Drop everything — full teardown (e.g. sign-out). */
  reset(): void {
    this.bySession.set(new Map());
    this.partialInputBySession.set(new Map());
  }
}

/** Immutably set `sessionId → toolUseId → value`, last write wins. */
function writeEntry<T>(
  map: ReadonlyMap<string, ReadonlyMap<string, T>>,
  sessionId: string,
  toolUseId: string,
  value: T,
): ReadonlyMap<string, ReadonlyMap<string, T>> {
  const next = new Map(map.get(sessionId) ?? []);
  next.set(toolUseId, value);
  return new Map(map).set(sessionId, next);
}
