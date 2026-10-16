// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { afterEach, describe, expect, it } from 'vitest';
import { FEATURES } from '../../services/features';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';
import { SHORTCUTS } from './shortcuts';
import { ShortcutsDialogComponent } from './shortcuts-dialog.component';

async function open(conversationSearch: boolean): Promise<HTMLElement> {
  TestBed.configureTestingModule({
    providers: [{ provide: FEATURES, useValue: { conversationSearch } }],
  });
  const { container } = await openInCdkDialog(ShortcutsDialogComponent, { ariaModal: true });
  return container;
}

function rowText(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('li')).map(li =>
    (li.querySelector('span')?.textContent ?? '').trim(),
  );
}

describe('ShortcutsDialogComponent', () => {
  afterEach(() => {
    TestBed.inject(Dialog).closeAll();
    TestBed.resetTestingModule();
  });

  it('is a named dialog', async () => {
    expectNamedDialog(await open(true), { name: 'Keyboard shortcuts' });
  });

  it('lists every shortcut in the table, in order', async () => {
    expect(rowText(await open(true))).toEqual(SHORTCUTS.map(s => s.description));
  });

  it('leaves search out when conversation search is off', async () => {
    const rows = rowText(await open(false));
    expect(rows).not.toContain('Search conversations');
    expect(rows).toHaveLength(SHORTCUTS.length - 1);
  });

  it('draws keycaps for sighted users and spells the keys out for screen readers', async () => {
    const row = (await open(true)).querySelector('li')!;
    const caps = row.querySelector('[aria-hidden="true"]')!;
    expect(caps.querySelectorAll('kbd').length).toBeGreaterThan(1);
    expect(row.querySelector('.sr-only')!.textContent).toMatch(/^(Command|Control) K$/);
  });
});
