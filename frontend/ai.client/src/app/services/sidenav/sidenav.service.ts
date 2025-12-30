import { Injectable, signal, computed } from '@angular/core';

/**
 * Service to manage sidenav open/close state.
 * Used for mobile/tablet view where sidenav is hidden by default,
 * and for desktop collapse functionality.
 */
@Injectable({
  providedIn: 'root'
})
export class SidenavService {
  // Mobile overlay state
  private isOpenSignal = signal(false);
  private isClosingSignal = signal(false);

  // Desktop collapsed state
  private isCollapsedSignal = signal(false);

  // Hidden state (for pages like login where sidenav should not appear at all)
  private isHiddenSignal = signal(false);

  /** Whether the mobile sidenav is currently open */
  readonly isOpen = this.isOpenSignal.asReadonly();

  /** Whether the sidenav is in the process of closing (for exit animation) */
  readonly isClosing = this.isClosingSignal.asReadonly();

  /** Whether the mobile sidenav should be visible (open or closing) */
  readonly isVisible = computed(() => this.isOpenSignal() || this.isClosingSignal());

  /** Whether the desktop sidenav is collapsed */
  readonly isCollapsed = this.isCollapsedSignal.asReadonly();

  /** Whether the sidenav is completely hidden (for login, etc.) */
  readonly isHidden = this.isHiddenSignal.asReadonly();

  /** Open the mobile sidenav */
  open(): void {
    this.isClosingSignal.set(false);
    this.isOpenSignal.set(true);
  }

  /** Close the mobile sidenav with animation */
  close(): void {
    if (!this.isOpenSignal()) return;

    this.isOpenSignal.set(false);
    this.isClosingSignal.set(true);

    // Wait for animation to complete before fully hiding
    setTimeout(() => {
      this.isClosingSignal.set(false);
    }, 300); // Match animation duration
  }

  /** Toggle the mobile sidenav open/close state */
  toggle(): void {
    if (this.isOpenSignal()) {
      this.close();
    } else {
      this.open();
    }
  }

  /** Toggle the desktop sidenav collapsed state */
  toggleCollapsed(): void {
    this.isCollapsedSignal.update(collapsed => !collapsed);
  }

  /** Collapse the desktop sidenav */
  collapse(): void {
    this.isCollapsedSignal.set(true);
  }

  /** Expand the desktop sidenav */
  expand(): void {
    this.isCollapsedSignal.set(false);
  }

  /** Hide the sidenav completely */
  hide(): void {
    this.isHiddenSignal.set(true);
  }

  /** Show the sidenav */
  show(): void {
    this.isHiddenSignal.set(false);
  }
}
