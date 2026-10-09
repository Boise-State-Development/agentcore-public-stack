import { Injectable, inject, signal } from '@angular/core';
import { Dialog, DialogRef } from '@angular/cdk/dialog';
import { FEATURES } from '../../services/features';

/** What the search dialog opens with. */
export interface SearchDialogData {
  /** A query to start from. */
  query?: string;
}

/** A request, made while the dialog is already up, to refocus it (and maybe re-query). */
export interface SearchDialogRefocus {
  query?: string;
  /** Bumped per request, so the same query asked twice still lands. */
  seq: number;
}

/**
 * Owns the one search dialog (`docs/specs/conversation-search.md` §6).
 *
 * `open()` while the dialog is up never stacks a second one: it refocuses the
 * input, adopting the new query when one is given. The dialog component is
 * imported on first open, so with `features.conversationSearch` off nothing of
 * it reaches the bundle a user downloads up front.
 */
@Injectable({ providedIn: 'root' })
export class SearchDialogService {
  private readonly dialog = inject(Dialog);
  private readonly enabled = inject(FEATURES).conversationSearch;

  private ref: DialogRef<unknown> | null = null;
  private opening: Promise<void> | null = null;

  private readonly refocusSignal = signal<SearchDialogRefocus | null>(null);

  /** Read by the open dialog; set when `open()` is called while it is already up. */
  readonly refocus = this.refocusSignal.asReadonly();

  /** Whether the search dialog is up (or on its way up). */
  isOpen(): boolean {
    return this.ref !== null || this.opening !== null;
  }

  /** Whether `ref` is the search dialog, for callers that look at every open dialog. */
  owns(ref: DialogRef<unknown>): boolean {
    return ref === this.ref;
  }

  async open(query?: string): Promise<void> {
    if (!this.enabled) return;
    if (this.ref) {
      this.refocusSignal.update(prev => ({ query, seq: (prev?.seq ?? 0) + 1 }));
      return;
    }
    if (this.opening) return this.opening;

    this.opening = (async () => {
      try {
        const { SearchDialogComponent } = await import('./search-dialog.component');
        const ref = this.dialog.open<unknown, SearchDialogData>(SearchDialogComponent, {
          data: { query },
          // The CDK container is the dialog element; it defaults aria-modal off.
          ariaLabel: 'Search',
          ariaModal: true,
        });
        this.ref = ref;
        ref.closed.subscribe(() => {
          if (this.ref === ref) this.ref = null;
        });
      } finally {
        this.opening = null;
      }
    })();
    return this.opening;
  }

  close(): void {
    this.ref?.close();
  }
}
