import { Injectable } from '@angular/core';

/** Unregisters what `register…` added. Call it from the component's `DestroyRef`. */
export type Unregister = () => void;

/** A response the copy shortcut can copy. */
export interface CopyableResponse {
  /** Whether it has text to copy yet (a response that so far only called tools has none). */
  canCopy(): boolean;
  /** Copies it; resolves whether anything reached the clipboard. */
  copy(): Promise<boolean>;
}

/**
 * The things the global shortcuts act on that live deep in a page: the
 * composer (Shift+Esc focuses it) and the latest response (Cmd/Ctrl+Shift+C
 * copies it).
 *
 * The components register themselves rather than the shell reaching into the
 * DOM, so the shortcut uses the same focus and copy paths as the click does.
 * More than one can be mounted at once (a page and an embedded preview); the
 * most recently registered one that is still on screen wins.
 */
@Injectable({ providedIn: 'root' })
export class ShortcutTargetsService {
  private readonly composers: (() => HTMLElement | undefined)[] = [];
  private readonly responses: CopyableResponse[] = [];

  /** `element` returns the composer's text field, or undefined while it has none. */
  registerComposer(element: () => HTMLElement | undefined): Unregister {
    return add(this.composers, element);
  }

  registerLatestResponse(response: CopyableResponse): Unregister {
    return add(this.responses, response);
  }

  /** Focuses the newest visible composer; false when there is none. */
  focusComposer(): boolean {
    for (let i = this.composers.length - 1; i >= 0; i--) {
      const el = this.composers[i]();
      if (el && isShown(el)) {
        el.focus();
        return true;
      }
    }
    return false;
  }

  /** Whether the newest registered response has text to copy. */
  canCopyLatestResponse(): boolean {
    return this.latestResponse()?.canCopy() ?? false;
  }

  /** Copies the newest registered response; false when there is none or the copy failed. */
  copyLatestResponse(): Promise<boolean> {
    return this.latestResponse()?.copy() ?? Promise.resolve(false);
  }

  private latestResponse(): CopyableResponse | undefined {
    return this.responses[this.responses.length - 1];
  }
}

function add<T>(list: T[], item: T): Unregister {
  list.push(item);
  return () => {
    const index = list.lastIndexOf(item);
    if (index >= 0) list.splice(index, 1);
  };
}

/** In the document and not inside something taken out of reach (`inert`, `hidden`). */
function isShown(el: HTMLElement): boolean {
  return el.isConnected && el.closest('[inert], [hidden]') === null;
}
