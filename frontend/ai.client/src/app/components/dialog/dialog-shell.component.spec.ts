import { describe, it, expect, beforeEach, vi } from 'vitest';
import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { DialogShellComponent } from './dialog-shell.component';

@Component({
  imports: [DialogShellComponent],
  template: `
    <app-dialog-shell [title]="title()" [description]="description()" [size]="size()" (closed)="closed()">
      <button dialogActions type="button">Act</button>
      <p>Body text</p>
      <div dialogFooter>Footer</div>
    </app-dialog-shell>
  `,
})
class Host {
  readonly title = signal('Members');
  readonly description = signal<string | null>('Who is in the project.');
  readonly size = signal<'md' | 'lg' | 'xl'>('lg');
  readonly closed = vi.fn();
}

describe('DialogShellComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ imports: [Host] });
  });

  function render() {
    const fixture = TestBed.createComponent(Host);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    return { fixture, el, host: fixture.componentInstance, panel: el.querySelector('[role=dialog]')! };
  }

  it('labels the dialog with its title and description, and projects the three slots', () => {
    const { el, panel } = render();
    const title = el.querySelector('h2')!;
    expect(title.textContent).toBe('Members');
    expect(panel.getAttribute('aria-labelledby')).toBe(title.id);
    expect(panel.getAttribute('aria-modal')).toBe('true');
    expect(el.querySelector(`#${panel.getAttribute('aria-describedby')}`)?.textContent).toBe('Who is in the project.');
    expect(el.textContent).toContain('Body text');
    expect(el.textContent).toContain('Footer');
    expect(el.querySelector('[dialogActions]')?.textContent).toBe('Act');
    expect(panel.className).toContain('sm:max-w-2xl');
  });

  it('drops the description reference when there is none', () => {
    const { fixture, host, panel } = render();
    host.description.set(null);
    fixture.detectChanges();
    expect(panel.getAttribute('aria-describedby')).toBeNull();
  });

  it('emits closed from the close button, Escape, and a click outside the panel', () => {
    const { el, host } = render();
    (el.querySelector('button[aria-label="Close dialog"]') as HTMLButtonElement).click();
    expect(host.closed).toHaveBeenCalledTimes(1);

    el.querySelector('app-dialog-shell')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(host.closed).toHaveBeenCalledTimes(2);

    const container = el.querySelector('[appDialogDismiss]') as HTMLElement;
    container.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    container.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(host.closed).toHaveBeenCalledTimes(3);
  });

  it('gives each shell its own title id', () => {
    const a = render().el.querySelector('h2')!.id;
    const b = render().el.querySelector('h2')!.id;
    expect(a).not.toBe(b);
  });
});
