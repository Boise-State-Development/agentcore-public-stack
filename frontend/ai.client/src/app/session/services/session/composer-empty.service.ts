import { Injectable, inject, signal } from '@angular/core';

import { ComposerDraftStorageService, StoredComposerDraft } from './composer-draft-storage.service';

/** What the composer holds for one conversation, reduced to what decides "empty". */
interface DraftShape {
  /** Typed text, a queued follow-up or an attached folder: always a draft. */
  composed: boolean;
  /** Upload ids of the files on the composer. */
  uploadIds: readonly string[];
}

function shapeOf(draft: StoredComposerDraft): DraftShape {
  return {
    composed: !!draft.text.trim() || (draft.queued?.length ?? 0) > 0 || (draft.folders?.length ?? 0) > 0,
    uploadIds: (draft.attachments ?? []).map((attachment) => attachment.uploadId),
  };
}

function sameShape(a: DraftShape | undefined, b: DraftShape): boolean {
  return !!a && a.composed === b.composed && a.uploadIds.length === b.uploadIds.length && a.uploadIds.every((id, i) => id === b.uploadIds[i]);
}

/**
 * Whether a conversation's composer holds nothing, as a signal, readable when that composer is
 * not on screen (docs/specs/workflow-designer.md §20.2, PR-D.5b).
 *
 * The draft itself is the composer's state plus `ComposerDraftStorageService`'s `localStorage`
 * row, and neither is reactive outside the composer. This holds, per draft key (a conversation's
 * id once it has one), whether anything was composed and which files are attached. The composer
 * writes it every time it mirrors its draft (`syncDraft`), so a keystroke lands here in the same
 * tick. A key the composer has not written in this tab is seeded from the stored draft on first
 * read, so "empty" after a reload is what the user left, not a guess.
 *
 * Empty means `isEmptyDraft`: no text, no queued follow-up, no file or folder attached. A caller
 * may pass files that don't count: a saved workflow's file is attached with `+` and shared with
 * the run from the conversation, and it stays on the composer after Start (dev, PR-D.5b-2), so
 * the merge ignores the run's own files. A waiting result card is not part of the draft; callers
 * that care read `DeliveryStateService` beside this.
 */
@Injectable({ providedIn: 'root' })
export class ComposerEmptyService {
  private readonly drafts = inject(ComposerDraftStorageService);
  private readonly known = signal<ReadonlyMap<string, DraftShape>>(new Map());

  /**
   * True when the composer for `key` holds nothing, apart from any of `ignoringUploads`.
   * Reactive: read it in a `computed` or `effect`.
   */
  isEmpty(key: string | null | undefined, ignoringUploads: ReadonlySet<string> = new Set()): boolean {
    if (!key) return true;
    const shape = this.known().get(key) ?? shapeOf(this.drafts.read(key));
    return !shape.composed && shape.uploadIds.every((id) => ignoringUploads.has(id));
  }

  /** The composer's own report of its draft for `key`. */
  note(key: string | null | undefined, draft: StoredComposerDraft): void {
    if (!key) return;
    const shape = shapeOf(draft);
    if (sameShape(this.known().get(key), shape)) return;
    this.known.update((map) => new Map(map).set(key, shape));
  }
}
