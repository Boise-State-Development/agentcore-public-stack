import { describe, it, expect, beforeEach } from 'vitest';
import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import { DialogDismissDirective } from './dialog-dismiss.directive';

/**
 * The behaviour every dialog in the app now depends on, tested once here rather than
 * twenty-four times.
 *
 * Driven through real DOM events on a real host, not by calling the handlers — the bug
 * this directive fixes was entirely about *which element receives the click*, so a test
 * that bypassed dispatch would have passed against the broken code too.
 */
@Component({
  imports: [DialogDismissDirective],
  template: `
    <div
      data-testid="overlay"
      appDialogDismiss
      (dismissed)="dismissCount = dismissCount + 1"
    >
      <div data-testid="panel">
        <textarea data-testid="field"></textarea>
      </div>
    </div>
  `,
})
class HostComponent {
  dismissCount = 0;
}

describe('DialogDismissDirective', () => {
  let host: HostComponent;
  let overlay: HTMLElement;
  let panel: HTMLElement;
  let field: HTMLElement;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ imports: [HostComponent] });
    const fixture = TestBed.createComponent(HostComponent);
    fixture.detectChanges();
    host = fixture.componentInstance;
    const el: HTMLElement = fixture.nativeElement;
    overlay = el.querySelector('[data-testid="overlay"]')!;
    panel = el.querySelector('[data-testid="panel"]')!;
    field = el.querySelector('[data-testid="field"]')!;
  });

  function press(target: HTMLElement): void {
    target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
  }

  function release(target: HTMLElement): void {
    target.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  }

  it('dismisses when the press and release both land on the overlay', () => {
    press(overlay);
    release(overlay);

    expect(host.dismissCount).toBe(1);
  });

  it('does not dismiss on a click inside the panel', () => {
    press(panel);
    release(panel);

    expect(host.dismissCount).toBe(0);
  });

  it('does not dismiss when a drag starts in the panel and ends on the overlay', () => {
    // Selecting text in a form field and releasing outside it. Dismissing here would
    // discard whatever the user had typed.
    press(field);
    release(overlay);

    expect(host.dismissCount).toBe(0);
  });

  it('does not stay armed after a drag that began inside the panel', () => {
    press(field);
    release(overlay);
    release(overlay);

    expect(host.dismissCount).toBe(0);
  });

  it('dismisses again on a fresh press after an ignored drag', () => {
    press(field);
    release(overlay);

    press(overlay);
    release(overlay);

    expect(host.dismissCount).toBe(1);
  });
});
