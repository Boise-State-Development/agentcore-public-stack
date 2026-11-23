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
 * The shell's own inputs. The output cards' specs cover the `lg` size they
 * all use; the attachment card's cover `md` and `sm`.
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

  it('lays an output card on its side: the tray on the left half, a two-line name beside it', async () => {
    const md = await render({});
    const lg = await render({ size: 'lg' });
    const article = (el: HTMLElement) => el.querySelector('article')!;
    const tray = (el: HTMLElement) => article(el).firstElementChild!.className;
    const foot = (el: HTMLElement) => article(el).lastElementChild!.className;

    expect(article(md).className).toContain('flex-col');
    expect(tray(md)).toContain('h-36');
    expect(foot(md)).toContain('border-t');

    expect(article(lg).className).toContain('flex-row');
    expect(tray(lg)).toContain('h-24');
    expect(tray(lg)).toContain('@md:w-1/2');
    expect(foot(lg)).toContain('border-l');
    expect(lg.querySelector('.border-b')).not.toBeNull();
    expect(lg.querySelector('p')?.className).toContain('line-clamp-2');
  });

  it('moves the meta line of a wide output card into its actions row, left of the actions', async () => {
    const md = await render({ meta: 'DOCX · 13 KB' });
    const lg = await render({ size: 'lg', meta: 'DOCX · 13 KB' });
    const metaLines = (el: HTMLElement) =>
      [...el.querySelectorAll('p')].filter((p) => p.textContent?.includes('DOCX · 13 KB'));

    expect(metaLines(md).length).toBe(1);
    const [underName, inRow] = metaLines(lg);
    expect(underName.className).toContain('@md:hidden');
    expect(inRow.className).toContain('@md:flex');
    expect(inRow.className).toContain('mr-auto');
    expect(inRow.parentElement?.className).toContain('@md:w-full');
  });

  it('marks a danger tone on the frame', async () => {
    const el = await render({ tone: 'danger' });
    expect(el.querySelector('article')?.className).toContain('ring-state-danger-300');
  });
});
