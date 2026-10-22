import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { Component, signal } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { DialogDescriptionDirective } from './dialog-description.directive';
import { DialogTitleDirective } from './dialog-title.directive';

@Component({
  imports: [DialogTitleDirective, DialogDescriptionDirective],
  template: `
    <div class="panel">
      <h2 appDialogTitle>Delete model?</h2>
      @if (described()) {
        <p appDialogDescription>This cannot be undone.</p>
      }
      <button type="button">Cancel</button>
    </div>
  `,
})
class Drawn {
  readonly described = signal(true);
}

@Component({
  imports: [DialogTitleDirective],
  template: `<h2 appDialogTitle dialogRole="alertdialog">Delete model?</h2>`,
})
class Confirm {}

describe('DialogTitleDirective / DialogDescriptionDirective', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  async function open<T>(component: new () => T, config: Record<string, unknown> = {}) {
    const ref = TestBed.inject(Dialog).open<unknown, unknown, T>(component, config);
    await new Promise(r => setTimeout(r, 0));
    const containers = document.querySelectorAll<HTMLElement>('.cdk-dialog-container');
    return { ref, container: containers[containers.length - 1] };
  }

  it('names and describes CDK’s container from the drawn title and description', async () => {
    const { container } = await open(Drawn);
    const title = container.querySelector('h2')!;
    expect(container.getAttribute('role')).toBe('dialog');
    expect(container.getAttribute('aria-labelledby')).toBe(title.id);
    expect(document.getElementById(container.getAttribute('aria-describedby')!)?.textContent).toBe(
      'This cannot be undone.',
    );
  });

  it('clears the description when it goes away and restores it when it returns', async () => {
    const { ref, container } = await open(Drawn);
    const drawn = ref.componentInstance!;
    drawn.described.set(false);
    ref.componentRef!.changeDetectorRef.detectChanges();
    expect(container.getAttribute('aria-describedby')).toBeNull();

    drawn.described.set(true);
    ref.componentRef!.changeDetectorRef.detectChanges();
    expect(container.querySelector(`#${container.getAttribute('aria-describedby')}`)?.tagName).toBe('P');
  });

  it('leaves an opener’s own name and description alone', async () => {
    const { container } = await open(Drawn, { ariaLabel: 'Opener name', ariaDescribedBy: 'opener-desc' });
    expect(container.getAttribute('aria-label')).toBe('Opener name');
    expect(container.getAttribute('aria-labelledby')).toBeNull();
    expect(container.getAttribute('aria-describedby')).toBe('opener-desc');
  });

  it('makes the container an alertdialog when the title asks for one', async () => {
    const { container } = await open(Confirm);
    expect(container.getAttribute('role')).toBe('alertdialog');
    expect(container.getAttribute('aria-labelledby')).toBe(container.querySelector('h2')!.id);
  });

  it('gives each title its own id, and only sets the id outside a dialog', () => {
    const a = TestBed.createComponent(Drawn);
    const b = TestBed.createComponent(Drawn);
    a.detectChanges();
    b.detectChanges();
    const titleA = (a.nativeElement as HTMLElement).querySelector('h2')!;
    const titleB = (b.nativeElement as HTMLElement).querySelector('h2')!;
    expect(titleA.id).toMatch(/^dialog-title-\d+$/);
    expect(titleA.id).not.toBe(titleB.id);
  });
});
