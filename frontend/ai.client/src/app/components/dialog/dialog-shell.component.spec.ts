import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { Component, inject, signal } from '@angular/core';
import { DIALOG_DATA, Dialog } from '@angular/cdk/dialog';
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

@Component({
  imports: [DialogShellComponent],
  template: `<app-dialog-shell title="Members" [description]="description" (closed)="0"><p>Body</p></app-dialog-shell>`,
})
class Opened {
  readonly description = inject<{ description: string | null }>(DIALOG_DATA).description;
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

  describe('opened through CDK Dialog', () => {
    afterEach(() => TestBed.inject(Dialog).closeAll());

    async function open(description: string | null, config: Record<string, unknown> = {}) {
      const ref = TestBed.inject(Dialog).open(Opened, { data: { description }, ...config });
      await new Promise(r => setTimeout(r, 0));
      const containers = document.querySelectorAll<HTMLElement>('.cdk-dialog-container');
      const container = containers[containers.length - 1];
      return { ref, container, title: container.querySelector('h2')! };
    }

    it('names CDK’s container with its title and description, and is the only dialog role', async () => {
      const { container, title } = await open('Who is in the project.');
      expect(container.getAttribute('role')).toBe('dialog');
      expect(container.getAttribute('aria-labelledby')).toBe(title.id);
      expect(document.getElementById(container.getAttribute('aria-describedby')!)?.textContent).toBe('Who is in the project.');
      expect(container.querySelectorAll('[role=dialog]').length).toBe(0);
      expect(container.querySelector('.dialog-panel')?.getAttribute('aria-modal')).toBeNull();
    });

    it('leaves the description off when there is none, and an opener’s own name and description alone', async () => {
      const bare = await open(null);
      expect(bare.container.getAttribute('aria-describedby')).toBeNull();
      bare.ref.close();
      await new Promise(r => setTimeout(r, 0));

      const named = await open('Shell text.', { ariaLabel: 'Opener name', ariaDescribedBy: 'opener-desc' });
      expect(named.container.getAttribute('aria-label')).toBe('Opener name');
      expect(named.container.getAttribute('aria-labelledby')).toBeNull();
      expect(named.container.getAttribute('aria-describedby')).toBe('opener-desc');
    });
  });
});
