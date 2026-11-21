import { Injectable, inject, signal } from '@angular/core';

import { ComposerDraftStorageService, StoredComposerDraft, isEmptyDraft } from './composer-draft-storage.service';

/**
 * Whether a conversation's composer holds nothing, as a signal, readable when that composer is
 * not on screen (docs/specs/workflow-designer.md §20.2, PR-D.5b).
 *
 * The draft itself is the composer's state plus `ComposerDraftStorageService`'s `localStorage`
 * row, and neither is reactive outside the composer. This holds one boolean per draft key (a
 * conversation's id once it has one), which the composer writes every time it mirrors its draft
 * (`syncDraft`), so a keystroke lands here in the same tick. A key the composer has not written in
 * this tab is seeded from the stored draft on first read, so "empty" after a reload is what the
 * user left, not a guess.
 *
 * Empty means `isEmptyDraft`: no text, no queued follow-up, no file or folder attached. A waiting
 * result card is not part of the draft; callers that care read `DeliveryStateService` beside this.
 */
@Injectable({ providedIn: 'root' })
export class ComposerEmptyService {
  private readonly drafts = inject(ComposerDraftStorageService);
  private readonly known = signal<ReadonlyMap<string, boolean>>(new Map());

  /** True when the composer for `key` holds nothing. Reactive: read it in a `computed` or `effect`. */
  isEmpty(key: string | null | undefined): boolean {
    if (!key) return true;
    const known = this.known().get(key);
    if (known !== undefined) return known;
    return isEmptyDraft(this.drafts.read(key));
  }

  /** The composer's own report of its draft for `key`. */
  note(key: string | null | undefined, draft: StoredComposerDraft): void {
    if (!key) return;
    const empty = isEmptyDraft(draft);
    if (this.known().get(key) === empty) return;
    this.known.update((map) => new Map(map).set(key, empty));
  }
}
