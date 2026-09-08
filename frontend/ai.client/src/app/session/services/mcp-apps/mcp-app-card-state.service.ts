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

/** Stable empty result so `cardsFor` misses don't churn change detection. */
const EMPTY_CARDS: readonly McpAppCard[] = Object.freeze([]);

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
 *
 * A card's `toolUseId` is the *originating* tool call — the one that
 * produced the App's `ui_resource` — not a per-call id (see
 * `McpAppProxyService.proxyToolCall`). So `byToolUse` groups every action
 * an App ran back onto the frame that ran them, which is where they
 * surface: behind the App frame's header, successes collapsed to a single
 * summary line. Cards whose frame can't render fall back to a standalone
 * box in the message list.
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
   * Cards grouped by the originating tool-use id (the App frame that ran
   * them), each group oldest-first. Computed once per card change rather
   * than filtered per frame, so a conversation with many App frames does
   * one pass instead of one-per-frame.
   */
  readonly byToolUse = computed<ReadonlyMap<string, McpAppCard[]>>(() => {
    const grouped = new Map<string, McpAppCard[]>();
    for (const card of this.cards()) {
      const group = grouped.get(card.toolUseId);
      if (group) group.push(card);
      else grouped.set(card.toolUseId, [card]);
    }
    return grouped;
  });

  /** Cards run by the App frame with this tool-use id, oldest-first. */
  cardsFor(toolUseId: string | undefined): readonly McpAppCard[] {
    if (!toolUseId) return EMPTY_CARDS;
    return this.byToolUse().get(toolUseId) ?? EMPTY_CARDS;
  }

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
