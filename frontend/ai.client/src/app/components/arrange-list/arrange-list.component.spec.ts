import { describe, it, expect, beforeEach } from 'vitest';
import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { ArrangeListComponent, ArrangeResult, ArrangeRow } from './arrange-list.component';

@Component({
  imports: [ArrangeListComponent],
  template: `<app-arrange-list label="Agents" [items]="items()" [allowHide]="allowHide()" (done)="result = $event" (cancelled)="cancelled = true" />`,
})
class HostComponent {
  readonly items = signal<ArrangeRow[]>([]);
  readonly allowHide = signal(true);
  result: ArrangeResult | null = null;
  cancelled = false;
}

describe('ArrangeListComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ imports: [HostComponent] });
  });

  function render(items: ArrangeRow[], allowHide = true) {
    const fixture = TestBed.createComponent(HostComponent);
    fixture.componentInstance.items.set(items);
    fixture.componentInstance.allowHide.set(allowHide);
    fixture.detectChanges();
    return fixture;
  }

  const button = (el: HTMLElement, label: string) => el.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)!;
  const doneButton = (el: HTMLElement) => [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Done')!;
  const names = (el: HTMLElement) =>
    [...el.querySelectorAll('[cdkDrag] span.font-medium, li.cdk-drag span.font-medium')].map(n => n.textContent?.trim());

  const ROWS: ArrangeRow[] = [
    { id: 'a', name: 'Alpha' },
    { id: 'b', name: 'Beta', hidden: true },
    { id: 'c', name: 'Gamma' },
  ];

  it('moves with the up and down buttons, announces it, and hands back the order on Done', () => {
    const fixture = render(ROWS);
    const el: HTMLElement = fixture.nativeElement;

    button(el, 'Move Gamma up').click();
    fixture.detectChanges();
    expect(names(el)).toEqual(['Alpha', 'Gamma', 'Beta']);
    expect(el.querySelector('[aria-live=polite]')?.textContent).toContain('Gamma moved to position 2 of 3.');
    expect(button(el, 'Move Alpha up').disabled).toBe(true);

    doneButton(el).click();
    expect(fixture.componentInstance.result?.ids).toEqual(['a', 'c', 'b']);
    expect([...fixture.componentInstance.result!.hiddenIds]).toEqual(['b']);
  });

  it('moves with the arrow keys and Home/End on the handle', () => {
    const fixture = render(ROWS);
    const el: HTMLElement = fixture.nativeElement;
    const handle = (name: string, i: number) =>
      button(el, `Reorder ${name}, position ${i} of 3. Use the arrow keys to move.`);

    handle('Alpha', 1).dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown' }));
    fixture.detectChanges();
    expect(names(el)).toEqual(['Beta', 'Alpha', 'Gamma']);
    handle('Gamma', 3).dispatchEvent(new KeyboardEvent('keydown', { key: 'Home' }));
    fixture.detectChanges();
    expect(names(el)).toEqual(['Gamma', 'Beta', 'Alpha']);
  });

  it('hides with the checkbox and offers none where a row cannot be hidden', () => {
    const fixture = render([...ROWS, { id: 'd', name: 'Delta', cannotHide: true }]);
    const el: HTMLElement = fixture.nativeElement;
    const box = el.querySelector<HTMLInputElement>('input[aria-label="Hide Alpha"]')!;
    box.click();
    fixture.detectChanges();
    expect(el.querySelector('input[aria-label="Hide Delta"]')).toBeNull();

    doneButton(el).click();
    expect([...fixture.componentInstance.result!.hiddenIds].sort()).toEqual(['a', 'b']);
  });

  it('keeps locked rows first and out of the result, and offers no Hide when not allowed', () => {
    const fixture = render([{ id: 'l', name: 'Locked', locked: true }, ...ROWS], false);
    const el: HTMLElement = fixture.nativeElement;
    expect(el.textContent).toContain('Pinned for your role and kept first');
    expect(el.querySelector('button[aria-label="Move Locked up"]')).toBeNull();
    expect(el.querySelectorAll('input[type=checkbox]')).toHaveLength(0);

    doneButton(el).click();
    expect(fixture.componentInstance.result?.ids).toEqual(['a', 'b', 'c']);
  });

  it('cancels on Cancel and on Escape without a result', () => {
    const fixture = render(ROWS);
    const el: HTMLElement = fixture.nativeElement;
    el.querySelector('app-arrange-list')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(fixture.componentInstance.cancelled).toBe(true);
    expect(fixture.componentInstance.result).toBeNull();
  });
});
