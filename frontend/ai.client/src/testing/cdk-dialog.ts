// cdk-dialog.ts — open a dialog component the way the app does, and check
// what assistive tech meets.
//
// CDK's `.cdk-dialog-container` is the `role="dialog"` element. It has no
// accessible name unless something names it (axe `aria-dialog-name`), so a
// spec that renders a dialog component directly with a stub `DialogRef` can't
// see the bug: there is no container. These open the component through a real
// `Dialog` instead. Close what you open in `afterEach`:
//
//   afterEach(() => TestBed.inject(Dialog).closeAll());
//
// Focus on open can't be asserted here: jsdom has no layout, so CDK's
// interactivity checker finds nothing focusable. Check it in a browser.

import { Type } from '@angular/core';
import { Dialog, DialogConfig, DialogRef, DialogRole } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expect } from 'vitest';

export interface OpenedDialog<C, R> {
  ref: DialogRef<R, C>;
  /** The `.cdk-dialog-container`: the element that carries the dialog role and name. */
  container: HTMLElement;
}

export async function openInCdkDialog<C, D = unknown, R = unknown>(
  component: Type<C>,
  config: DialogConfig<D, DialogRef<R, C>> = {},
): Promise<OpenedDialog<C, R>> {
  const ref = TestBed.inject(Dialog).open<R, D, C>(component, config);
  await new Promise(resolve => setTimeout(resolve, 0));
  const containers = document.querySelectorAll<HTMLElement>('.cdk-dialog-container');
  return { ref, container: containers[containers.length - 1] };
}

function textOf(id: string | null): string | null {
  const element = id ? document.getElementById(id) : null;
  return element ? (element.textContent ?? '').replace(/\s+/g, ' ').trim() : null;
}

/**
 * The container has the role, its `aria-labelledby` resolves to a heading inside it with
 * the expected text, and nothing inside draws a second dialog role or `aria-modal`.
 */
export function expectNamedDialog(
  container: HTMLElement,
  expected: { name: string | RegExp; role?: DialogRole; description?: string | RegExp },
): void {
  expect(container.getAttribute('role')).toBe(expected.role ?? 'dialog');

  const labelId = container.getAttribute('aria-labelledby');
  const label = labelId ? document.getElementById(labelId) : null;
  expect(label, 'aria-labelledby resolves to an element in the dialog').not.toBeNull();
  expect(container.contains(label)).toBe(true);
  expect(textOf(labelId)).toMatch(expected.name);

  if (expected.description !== undefined) {
    expect(textOf(container.getAttribute('aria-describedby'))).toMatch(expected.description);
  }

  expect(container.querySelectorAll('[role=dialog], [role=alertdialog]').length).toBe(0);
  expect(container.querySelectorAll('[aria-modal]').length).toBe(0);
}
