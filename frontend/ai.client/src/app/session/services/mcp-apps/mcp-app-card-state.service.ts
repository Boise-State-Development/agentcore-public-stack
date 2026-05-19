import { Injectable, computed, signal } from '@angular/core';

/**
 * A persisted app-initiated tool-call card (MCP Apps PR #6, Option A).
 * Mirrors the backend `card_store` record (key attrs stripped).
 */
export interface McpAppCard {
  cardId: string;
  toolUseId: string;
  toolName: string;
  arguments: Record<string, unknown>;
  content: Array<Record<string, unknown>>;
  isError: boolean;
  createdAt: string;
  producedByMessageIndex?: number | null;
}

/**
 * Reload hydration for app-initiated tool-call cards (MCP Apps PR #6).
 *
 * PR #5's broker is in-memory: an App's `tools/call` surfaces *live* as
 * `tool_use`/`tool_result` in the thread, but those synthesized events are
 * never persisted to AgentCore Memory (persisting a synthetic tool turn
 * would break Bedrock role alternation). So on a page reload they vanish.
 * Option A closes that gap exactly like the Artifacts feature: app-api
 * persists a side-channel provenance record and the SPA replays it here as
 * a **static historical card** (the App iframe itself is not
 * re-instantiated on reload — the realistic target is a read-only record).
 *
 * Structural sibling of `ArtifactStateService` / `McpAppStateService`: a
 * session-load `seedFromHydration` path and a `reset()` the session page
 * calls on conversation change. There is deliberately no `recordLive`:
 * live app-initiated calls already render through the normal tool path
 * (PR #5), so a live card would double-render.
 */
@Injectable({ providedIn: 'root' })
export class McpAppCardStateService {
  private readonly byId = signal<ReadonlyMap<string, McpAppCard>>(new Map());

  /** Cards for the current conversation, oldest-first (stable order). */
  readonly cards = computed<McpAppCard[]>(() =>
    [...this.byId().values()].sort((a, b) =>
      a.createdAt < b.createdAt ? -1 : a.createdAt > b.createdAt ? 1 : 0,
    ),
  );

  readonly hasCards = computed(() => this.byId().size > 0);

  /**
   * Seed cards fetched from the app-api list endpoint on conversation
   * load. Non-clobbering by `cardId` so a slow response can't undo state
   * (matches `ArtifactStateService.seedFromHydration` semantics).
   */
  seedFromHydration(list: readonly McpAppCard[]): void {
    if (!list.length) return;
    const next = new Map(this.byId());
    for (const card of list) {
      if (!next.has(card.cardId)) next.set(card.cardId, card);
    }
    this.byId.set(next);
  }

  /** Drop all cards — called on conversation change. */
  reset(): void {
    if (this.byId().size) this.byId.set(new Map());
  }
}
