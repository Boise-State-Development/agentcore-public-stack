import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { MemoryLinkPickerDialogComponent } from './memory-link-picker-dialog.component';
import { memoryEntry } from '../../../testing/project-memory.fixtures';

describe('MemoryLinkPickerDialogComponent', () => {
  const dialogRef = { close: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    TestBed.configureTestingModule({
      imports: [MemoryLinkPickerDialogComponent],
      providers: [
        { provide: DialogRef, useValue: dialogRef },
        {
          provide: DIALOG_DATA,
          useValue: {
            entries: [
              memoryEntry('sis', { aliases: ['banner'] }),
              memoryEntry('rates', { description: 'Throttling limits' }),
              memoryEntry('deadlines'),
            ],
            current: 'deadlines',
          },
        },
      ],
    });
  });

  function render() {
    const fixture = TestBed.createComponent(MemoryLinkPickerDialogComponent);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const input = el.querySelector<HTMLInputElement>('[role=combobox]')!;
    const options = () => Array.from(el.querySelectorAll('[role=option]'));
    const key = (k: string) => {
      input.dispatchEvent(new KeyboardEvent('keydown', { key: k }));
      fixture.detectChanges();
    };
    const type = (value: string) => {
      input.value = value;
      input.dispatchEvent(new Event('input'));
      fixture.detectChanges();
    };
    return { el, input, options, key, type };
  }

  it('lists the index and every other file, the first one active', () => {
    const { input, options } = render();
    expect(options().map(o => o.querySelector('.font-mono')?.textContent?.trim())).toEqual(['MEMORY.md', 'sis', 'rates']);
    expect(input.getAttribute('aria-activedescendant')).toBe(options()[0].id);
    expect(options()[0].getAttribute('aria-selected')).toBe('true');
  });

  it('filters on names, aliases and descriptions, and announces the count', () => {
    const { el, options, type } = render();
    type('banner');
    expect(options().map(o => o.querySelector('.font-mono')?.textContent?.trim())).toEqual(['sis']);
    type('throttl');
    expect(options().map(o => o.querySelector('.font-mono')?.textContent?.trim())).toEqual(['rates']);
    expect(el.querySelector('[aria-live=polite]')?.textContent).toContain('1 file matches');
    type('zzz');
    expect(el.textContent).toContain('No file matches “zzz”.');
  });

  it('moves with the arrow keys, wrapping, and picks with Enter', () => {
    const { input, options, key } = render();
    key('ArrowDown');
    key('ArrowDown');
    expect(input.getAttribute('aria-activedescendant')).toBe(options()[2].id);
    key('ArrowDown');
    expect(input.getAttribute('aria-activedescendant')).toBe(options()[0].id);
    key('ArrowUp');
    key('Enter');
    expect(dialogRef.close).toHaveBeenCalledWith('rates');
  });

  it('closes with nothing when cancelled', () => {
    const { el } = render();
    (el.querySelector('button[aria-label="Close dialog"]') as HTMLButtonElement).click();
    expect(dialogRef.close).toHaveBeenCalledWith(undefined);
  });
});
