import { Injectable, signal } from '@angular/core';
import { DeliveryView, SendBackReview } from './handoff.models';

/** A card on a composer: the delivery, plus the user's edit if they made one. */
export interface ComposerCard extends DeliveryView {
  /** The card's text as the user edited it; absent while unedited. */
  editedText?: string;
  /** A run result's edited parts, by step id; a part not listed is unedited (PR-W.4b). */
  editedParts?: Readonly<Record<string, string>>;
}

/** What the next send carries for one card (`deliveries` on `/invocations`). */
export interface DeliveryAttach {
  deliveryId: string;
  /** Present only when the card was edited, as the Runtime expects. */
  text?: string;
  /** A run result's edited parts only (workflow-runs spec §19 PR-W.2); absent when none was edited. */
  parts?: { stepId: string; text: string }[];
}

/** At most this many cards ride one send; the rest wait for the next (handoffs spec §4.8). */
export const DELIVERY_MAX_PER_TURN = 3;

/**
 * Deliveries the SPA knows about, per conversation (handoffs spec §4.3, §4.8, PR-0.4).
 *
 * State only, like `HandoffStateService`, so `MessageMapService` can seed it from `GET /messages`
 * and the composer can read it without an injection cycle. The actions live in `DeliveryService`.
 *
 * - **cards**, by receiving session: the deliveries waiting on that conversation's composer. A card
 *   rides the next send by default, because the user made it; Remove takes it off.
 * - **reviews**, by target session: Send to *{title}*'s open review sheet, at most one per thread.
 */
@Injectable({ providedIn: 'root' })
export class DeliveryStateService {
  private readonly cardsBySession = signal<ReadonlyMap<string, readonly ComposerCard[]>>(new Map());
  private readonly reviewsBySession = signal<ReadonlyMap<string, SendBackReview>>(new Map());

  cardsFor(sessionId: string | null | undefined): readonly ComposerCard[] {
    if (!sessionId) return [];
    return this.cardsBySession().get(sessionId) ?? [];
  }

  reviewFor(sessionId: string | null | undefined): SendBackReview | undefined {
    if (!sessionId) return undefined;
    return this.reviewsBySession().get(sessionId);
  }

  /**
   * Seed from a `GET /messages` response: the server's pending cards, oldest first. An edit the
   * user made in this tab survives a reload of the list, so a background refresh never throws it away.
   */
  hydrate(sessionId: string, deliveries: readonly DeliveryView[] | undefined): void {
    const server = deliveries ?? [];
    this.cardsBySession.update((map) => {
      const local = map.get(sessionId) ?? [];
      if (server.length === 0 && local.length === 0) return map;
      const edits = new Map(local.map((card) => [card.deliveryId, card]));
      const merged = sortCards(
        server.map((d) => ({
          ...d,
          editedText: edits.get(d.deliveryId)?.editedText,
          editedParts: edits.get(d.deliveryId)?.editedParts,
        })),
      );
      return new Map(map).set(sessionId, merged);
    });
  }

  /** A card made in this tab (Bring back result), shown before any reload lists it. */
  add(sessionId: string, card: DeliveryView): void {
    this.cardsBySession.update((map) => {
      const cards = (map.get(sessionId) ?? []).filter((c) => c.deliveryId !== card.deliveryId);
      return new Map(map).set(sessionId, sortCards([...cards, card]));
    });
  }

  remove(sessionId: string, deliveryId: string): void {
    this.cardsBySession.update((map) => {
      const cards = map.get(sessionId);
      if (!cards?.some((c) => c.deliveryId === deliveryId)) return map;
      return new Map(map).set(sessionId, cards.filter((c) => c.deliveryId !== deliveryId));
    });
  }

  /** The user's edit; text equal to the delivery's own reads as unedited. */
  edit(sessionId: string, deliveryId: string, text: string): void {
    this.cardsBySession.update((map) => {
      const cards = map.get(sessionId);
      if (!cards) return map;
      const next = cards.map((c) =>
        c.deliveryId === deliveryId ? { ...c, editedText: text === c.text ? undefined : text } : c,
      );
      return new Map(map).set(sessionId, next);
    });
  }

  /** The user's edit of one part of a run result; text equal to the part's own reads as unedited. */
  editPart(sessionId: string, deliveryId: string, stepId: string, text: string): void {
    this.cardsBySession.update((map) => {
      const cards = map.get(sessionId);
      if (!cards) return map;
      const next = cards.map((c) => {
        if (c.deliveryId !== deliveryId) return c;
        const part = c.parts?.find((p) => p.stepId === stepId);
        if (!part || part.status !== 'done') return c;
        const edited = { ...(c.editedParts ?? {}) };
        if (text === part.text) delete edited[stepId];
        else edited[stepId] = text;
        return { ...c, editedParts: Object.keys(edited).length ? edited : undefined };
      });
      return new Map(map).set(sessionId, next);
    });
  }

  /**
   * Take the cards the next send carries off the composer, oldest first and at most
   * `DELIVERY_MAX_PER_TURN`. The rest stay for the send after. `restore` puts them back when the
   * send never reached the server.
   */
  takeForSend(sessionId: string): { attach: DeliveryAttach[]; cards: ComposerCard[] } {
    const cards = this.cardsFor(sessionId).slice(0, DELIVERY_MAX_PER_TURN);
    if (cards.length === 0) return { attach: [], cards: [] };
    const taken = new Set(cards.map((c) => c.deliveryId));
    this.cardsBySession.update((map) =>
      new Map(map).set(sessionId, (map.get(sessionId) ?? []).filter((c) => !taken.has(c.deliveryId))),
    );
    return { attach: cards.map(attachFor), cards };
  }

  restore(sessionId: string, cards: readonly ComposerCard[]): void {
    if (cards.length === 0) return;
    this.cardsBySession.update((map) => {
      const current = map.get(sessionId) ?? [];
      const known = new Set(current.map((c) => c.deliveryId));
      return new Map(map).set(sessionId, sortCards([...current, ...cards.filter((c) => !known.has(c.deliveryId))]));
    });
  }

  setReview(sessionId: string, review: SendBackReview): void {
    this.reviewsBySession.update((map) => new Map(map).set(sessionId, review));
  }

  updateReview(sessionId: string, patch: Partial<SendBackReview>): void {
    const current = this.reviewsBySession().get(sessionId);
    if (current) this.setReview(sessionId, { ...current, ...patch });
  }

  clearReview(sessionId: string): void {
    this.reviewsBySession.update((map) => {
      if (!map.has(sessionId)) return map;
      const next = new Map(map);
      next.delete(sessionId);
      return next;
    });
  }
}

/** One card as a send carries it: a run result names its edited parts, a plain card its edited text. */
function attachFor(card: ComposerCard): DeliveryAttach {
  if (card.parts) {
    const parts = card.parts
      .filter((part) => card.editedParts?.[part.stepId] !== undefined)
      .map((part) => ({ stepId: part.stepId, text: card.editedParts![part.stepId] }));
    return parts.length ? { deliveryId: card.deliveryId, parts } : { deliveryId: card.deliveryId };
  }
  return card.editedText === undefined ? { deliveryId: card.deliveryId } : { deliveryId: card.deliveryId, text: card.editedText };
}

function sortCards<T extends DeliveryView>(cards: T[]): T[] {
  return [...cards].sort((a, b) => a.createdAt.localeCompare(b.createdAt));
}
