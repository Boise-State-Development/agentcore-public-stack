import { describe, expect, it } from 'vitest';
import { TestBed } from '@angular/core/testing';

import { ContentCardComponent } from './content-card.component';

async function render(inputs: Record<string, unknown>): Promise<HTMLElement> {
  TestBed.resetTestingModule();
  await TestBed.configureTestingModule({ imports: [ContentCardComponent] }).compileComponents();
  const fixture = TestBed.createComponent(ContentCardComponent);
  for (const [name, value] of Object.entries({ title: 'Report.pdf', ...inputs })) {
    fixture.componentRef.setInput(name, value);
  }
  fixture.detectChanges();
  await fixture.whenStable();
  return fixture.nativeElement as HTMLElement;
}

/**
 * The shell's own inputs. The browser card's specs cover the conversation size
 * (`md`, no bar, default tone), which is all it uses.
 */
describe('ContentCardComponent', () => {
  it('names the card and joins meta and status, with a pause for screen readers', async () => {
    const el = await render({ meta: 'PDF · 2 MB', status: 'Preparing…', label: 'Report.pdf, preparing' });
    expect(el.querySelector('article')?.getAttribute('aria-label')).toBe('Report.pdf, preparing');
    expect(el.textContent).toContain('PDF · 2 MB');
    expect(el.querySelector('.sr-only')?.textContent).toBe(', ');
    expect(el.textContent).toContain('Preparing…');
  });

  it('draws a progress bar only when given one', async () => {
    expect((await render({})).querySelector('[data-testid="content-card-progress"]')).toBeNull();
    const bar = (await render({ progress: 40 })).querySelector<HTMLElement>(
      '[data-testid="content-card-progress"] > div',
    );
    expect(bar?.style.width).toBe('40%');
  });

  it('drops the chrome row at the composer size', async () => {
    const md = await render({});
    const sm = await render({ size: 'sm' });
    expect(md.querySelector('.border-b')).not.toBeNull();
    expect(sm.querySelector('.border-b')).toBeNull();
  });

  it('marks a danger tone on the frame', async () => {
    const el = await render({ tone: 'danger' });
    expect(el.querySelector('article')?.className).toContain('ring-state-danger-300');
  });
});
