import { Injectable, computed, inject, signal } from '@angular/core';
import { SidenavService } from '../../../services/sidenav/sidenav.service';

/** Which feature currently owns the right-docked rail. */
export type DockedPaneOwner = 'artifact' | 'file-preview';

/**
 * The single right-docked rail, shared by every feature that wants to
 * park a pane next to the chat.
 *
 * Extracted from `ArtifactStateService` when the .docx preview became a
 * second pane. The width and the side-nav choreography were never
 * artifact concerns — they are properties of *the rail*, and there is
 * only one of it. The layout reserves exactly one gutter
 * (`--artifact-pane-width` + the `artifact-pane-open` class), so two
 * panes open at once would overlap and the chat column would be sized
 * for whichever service happened to answer first.
 *
 * Mutual exclusion is therefore enforced here rather than trusted to
 * callers: `claim()` hands the rail to one owner and implicitly evicts
 * whoever held it. Evicted owners are not called back — instead each
 * gates its public open-ref through `owner()` (see
 * `ArtifactStateService.openArtifact`), so eviction is a read, not a
 * notification. That keeps the dependency one-way and rules out the
 * write-loop an effect-based handoff would invite.
 */
@Injectable({ providedIn: 'root' })
export class DockedPaneService {
  private readonly sidenav = inject(SidenavService);

  private readonly ownerSignal = signal<DockedPaneOwner | null>(null);

  /** Side-nav collapsed state captured the moment the rail opened, so a
   *  user who had the nav open isn't left with it collapsed after the
   *  pane closes (and one who had it collapsed keeps it that way). */
  private navWasCollapsed = false;

  /** User-controlled rail width in px. Shared with the layout (content
   *  padding + fixed footer/topnav offset) via a CSS var so the chat
   *  never ends up under the pane. Survives open/close and survives a
   *  handoff between owners — the rail keeps the width the user set,
   *  whatever is being shown in it. Resets on full reload. */
  private static readonly MIN_WIDTH = 360;
  private static readonly MAX_WIDTH = 1200;
  private static readonly DEFAULT_WIDTH = 672; // 42rem, the original fixed size
  private readonly widthSignal = signal(DockedPaneService.DEFAULT_WIDTH);

  /** Which feature holds the rail, or null when it is closed. */
  readonly owner = this.ownerSignal.asReadonly();

  /** True while any pane is docked — the layout's gutter trigger. */
  readonly isOpen = computed(() => this.ownerSignal() !== null);

  /** Current rail width in px (clamped). */
  readonly width = this.widthSignal.asReadonly();

  get widthMin(): number {
    return DockedPaneService.MIN_WIDTH;
  }

  get widthMax(): number {
    return DockedPaneService.MAX_WIDTH;
  }

  /** Set the rail width, clamped to the allowed range. The caller owns
   *  any viewport-relative ceiling — this service knows absolute bounds,
   *  not the window size. */
  setWidth(px: number): void {
    const clamped = Math.min(
      DockedPaneService.MAX_WIDTH,
      Math.max(DockedPaneService.MIN_WIDTH, Math.round(px)),
    );
    this.widthSignal.set(clamped);
  }

  /** Whether `owner` currently holds the rail. */
  holds(owner: DockedPaneOwner): boolean {
    return this.ownerSignal() === owner;
  }

  /**
   * Give the rail to `owner`, evicting the current holder if different.
   *
   * The side nav is collapsed only on a genuine closed -> open
   * transition: re-claiming for the same owner (switching artifact
   * version, switching previewed file) or handing over between owners
   * must not overwrite the captured nav state, because by then the nav
   * is collapsed by us and we would restore the wrong thing.
   */
  claim(owner: DockedPaneOwner): void {
    if (this.ownerSignal() === null) {
      this.navWasCollapsed = this.sidenav.isCollapsed();
      this.sidenav.collapse();
    }
    this.ownerSignal.set(owner);
  }

  /** Close the rail if `owner` holds it; a no-op otherwise, so a stale
   *  close from an already-evicted owner cannot shut the current pane. */
  release(owner: DockedPaneOwner): void {
    if (this.ownerSignal() !== owner) return;
    this.ownerSignal.set(null);
    this.restoreNav();
  }

  /** Close the rail whoever holds it — session change and teardown. */
  releaseAll(): void {
    if (this.ownerSignal() === null) return;
    this.ownerSignal.set(null);
    this.restoreNav();
  }

  /** Return the side nav to whatever state it was in before the rail
   *  opened. If the user had it collapsed already, leave it collapsed. */
  private restoreNav(): void {
    if (!this.navWasCollapsed) this.sidenav.expand();
  }
}
