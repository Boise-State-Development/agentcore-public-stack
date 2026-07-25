import { Injectable, computed, inject, signal } from '@angular/core';
import { AgentService } from './agent.service';
import { AgentPinService } from './agent-pin.service';

/** One row in the `@` menu (D11). */
export interface MentionableAgent {
  agentId: string;
  name: string;
  /** Rendered as secondary text: the publisher for a pinned agent, else the tagline. */
  subtitle?: string;
  emoji?: string;
  iconUrl?: string;
  group: 'own' | 'pinned';
}

/** How many rows the menu shows at once. */
const MAX_RESULTS = 8;

/**
 * The `@`-mention candidate list (Marketplace D11).
 *
 * **Scope is deliberately narrow: your own Agents plus everything pinned.** Making the menu
 * search the whole store turns an autocomplete into a directory query — which is what the
 * store page is for — and it puts every Agent's name in front of every user in a surface
 * where they cannot evaluate it. The menu's last row is "Browse all agents →" instead.
 *
 * Pinned deliberately includes role-seeded pins (D9), which is the point of seeding a role:
 * a member who has never visited the store still finds their department's Agent by typing
 * `@`. That comes free — `AgentPinService.pins()` is already the resolved effective list.
 *
 * Both sources are **already loaded by other surfaces**, and both are session-cached, so the
 * menu costs nothing on the keystroke path. A source that fails is simply absent: an `@`
 * menu missing a group is still usable, and the composer must never fail because a list
 * behind an optional affordance did not load.
 */
@Injectable({ providedIn: 'root' })
export class AgentMentionService {
  private agentService = inject(AgentService);
  private pinService = inject(AgentPinService);

  private loaded = false;
  private _loading = signal(false);

  readonly loading = this._loading.asReadonly();

  /**
   * Own Agents first, then pins that are not already listed as your own.
   *
   * Dedup keeps the *own* row: an author who pinned their own Agent should see it once,
   * under the group that explains why they can edit it. Drafts are excluded — an Agent whose
   * instructions and bindings are half-written should not be handed a turn.
   */
  readonly mentionable = computed<MentionableAgent[]>(() => {
    const own = this.agentService
      .agents$()
      .filter((agent) => agent.status === 'COMPLETE')
      .map<MentionableAgent>((agent) => ({
        agentId: agent.agentId,
        name: agent.name,
        subtitle: agent.tagline || agent.description,
        emoji: agent.emoji,
        iconUrl: agent.iconUrl,
        group: 'own',
      }));

    const ownIds = new Set(own.map((agent) => agent.agentId));
    const pinned = this.pinService
      .pins()
      .filter((pin) => !ownIds.has(pin.agentId))
      .map<MentionableAgent>((pin) => ({
        agentId: pin.agentId,
        name: pin.name,
        subtitle: pin.publisher?.label || pin.tagline,
        emoji: pin.emoji,
        iconUrl: pin.iconUrl,
        group: 'pinned',
      }));

    return [...own, ...pinned];
  });

  /**
   * Warm both lists. Safe to call on every composer focus — each underlying service
   * caches for the session, and a failure in one leaves the other's rows usable.
   */
  async load(): Promise<void> {
    if (this.loaded) return;
    this.loaded = true;
    this._loading.set(true);
    try {
      await Promise.all([
        // Drafts excluded at the source as well as in the projection: an agent that is
        // still a draft is not a thing you can hand a turn to.
        this.agentService.loadAgents(false).catch(() => undefined),
        this.pinService.load().catch(() => undefined),
      ]);
    } finally {
      this._loading.set(false);
    }
  }

  /**
   * Rows matching what the user has typed after the `@`.
   *
   * Name matches rank above subtitle matches, and a prefix above a substring, because the
   * thing a person types after `@` is almost always the start of a name. Capped at
   * {@link MAX_RESULTS} — a menu you have to scroll is a list, and the list has a page.
   */
  search(query: string): MentionableAgent[] {
    const needle = query.trim().toLowerCase();
    const all = this.mentionable();
    if (!needle) return all.slice(0, MAX_RESULTS);

    const scored = all
      .map((agent) => ({ agent, score: this.score(agent, needle) }))
      .filter((entry) => entry.score > 0)
      .sort((a, b) => b.score - a.score);

    return scored.slice(0, MAX_RESULTS).map((entry) => entry.agent);
  }

  private score(agent: MentionableAgent, needle: string): number {
    const name = agent.name.toLowerCase();
    if (name.startsWith(needle)) return 3;
    if (name.includes(needle)) return 2;
    if ((agent.subtitle ?? '').toLowerCase().includes(needle)) return 1;
    return 0;
  }
}
