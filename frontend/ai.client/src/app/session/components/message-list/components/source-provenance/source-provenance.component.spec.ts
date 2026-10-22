import { describe, it, expect, beforeEach } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { SourceProvenanceComponent } from './source-provenance.component';
import type { SourceProvenanceClaim } from '../../../../../shared/utils/stream-parser';

function claim(over: Partial<SourceProvenanceClaim> = {}): SourceProvenanceClaim {
  return {
    id: 'total_award',
    state: 'supported',
    method: 'literal',
    value: '$487,500',
    quote: 'The total approved budget is $487,500 for this award.',
    locator: { kind: 'page', value: 1, precision: 'measured' },
    uploadId: 'up-1',
    filename: 'award.pdf',
    ...over,
  };
}

async function render(
  claims: SourceProvenanceClaim[],
): Promise<ComponentFixture<SourceProvenanceComponent>> {
  // Reset per render: TestBed refuses reconfiguration once instantiated, and
  // some cases below render several times.
  TestBed.resetTestingModule();
  await TestBed.configureTestingModule({
    imports: [SourceProvenanceComponent],
  }).compileComponents();
  const fixture = TestBed.createComponent(SourceProvenanceComponent);
  fixture.componentRef.setInput('claims', claims);
  await fixture.whenStable();
  fixture.detectChanges();
  return fixture;
}

const text = (f: ComponentFixture<SourceProvenanceComponent>) =>
  (f.nativeElement as HTMLElement).textContent ?? '';

describe('SourceProvenanceComponent', () => {
  beforeEach(() => TestBed.resetTestingModule());

  it('renders nothing when there are no claims', async () => {
    const fixture = await render([]);
    expect((fixture.nativeElement as HTMLElement).textContent?.trim()).toBe('');
  });

  describe('a clean run', () => {
    it('summarises in one line and stays collapsed', async () => {
      const fixture = await render([claim(), claim({ id: 'start' })]);
      expect(text(fixture)).toContain('Confirmed 2 values');
      expect(text(fixture)).toContain('award.pdf');
      const toggle = (fixture.nativeElement as HTMLElement).querySelector('button')!;
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      // The rows animate open rather than being added to the DOM, so the
      // quotes are present but must not be exposed: they are passages from
      // the user's document, and a screen reader should not read out ones
      // nobody opened.
      const host = fixture.nativeElement as HTMLElement;
      const panel = host.querySelector('.collapsible-content')!;
      expect(panel.getAttribute('aria-hidden')).toBe('true');
      expect(panel.hasAttribute('inert')).toBe(true);
    });

    it('expands to the detail on click', async () => {
      const fixture = await render([claim()]);
      const toggle = (fixture.nativeElement as HTMLElement).querySelector('button')!;
      toggle.click();
      fixture.detectChanges();
      expect(toggle.getAttribute('aria-expanded')).toBe('true');
      const panel = (fixture.nativeElement as HTMLElement).querySelector('.collapsible-content')!;
      expect(panel.getAttribute('aria-hidden')).toBeNull();
      expect(panel.hasAttribute('inert')).toBe(false);
      expect(text(fixture)).toContain('The total approved budget is $487,500');
      expect(text(fixture)).toContain('found on page 1');
    });
  });

  describe('quote_unsupported — the state the feature exists for', () => {
    const fabricated = claim({
      id: 'total_award',
      state: 'quote_unsupported',
      method: 'no_match',
      value: '$912,000',
    });

    it('opens itself, because a warning behind a click is not a warning', async () => {
      const fixture = await render([fabricated]);
      const toggle = (fixture.nativeElement as HTMLElement).querySelector('button')!;
      expect(toggle.getAttribute('aria-expanded')).toBe('true');
      expect(text(fixture)).toContain('1 unconfirmed');
    });

    it('names the value it cannot confirm', async () => {
      const fixture = await render([fabricated]);
      expect(text(fixture)).toContain('$912,000');
    });

    it('never renders a bare page reference for it', async () => {
      const fixture = await render([fabricated]);
      // The locator appears only inside a sentence that says the value is not
      // in the passage. A bare "p. 1" would read as a confirmed citation.
      expect(text(fixture)).toContain('not in the quoted passage on page 1');
      expect(text(fixture)).not.toMatch(/\bp\.\s*1\b/);
    });

    it('dominates the headline even when most claims are fine', async () => {
      const fixture = await render([claim(), claim({ id: 'b' }), fabricated]);
      expect(text(fixture)).toContain('1 unconfirmed');
    });
  });

  describe('the states that are absences of evidence', () => {
    it('says a located passage was not checked, for unassessed', async () => {
      const fixture = await render([
        claim({ state: 'unassessed', method: 'enum_field', value: 'Yes' }),
      ]);
      (fixture.nativeElement as HTMLElement).querySelector('button')!.click();
      fixture.detectChanges();
      expect(text(fixture)).toContain('passage found on page 1, value not checked');
    });

    it('says there is no matching passage, for unverified', async () => {
      const fixture = await render([
        claim({ state: 'unverified', method: 'quote_not_located', locator: null }),
      ]);
      (fixture.nativeElement as HTMLElement).querySelector('button')!.click();
      fixture.detectChanges();
      expect(text(fixture)).toContain('no matching passage in the document');
    });

    it('does not count them as confirmed in the headline', async () => {
      const fixture = await render([
        claim(),
        claim({ id: 'b', state: 'unverified', locator: null }),
      ]);
      expect(text(fixture)).toContain('Confirmed 1 of 2 values');
    });

    it('does not treat them as warnings', async () => {
      // Only `quote_unsupported` earns the danger count in the header: an
      // absence of evidence is not a claim that the answer is wrong.
      const fixture = await render([claim({ state: 'unverified', locator: null })]);
      expect(text(fixture)).not.toContain('unconfirmed');
      const dot = (fixture.nativeElement as HTMLElement).querySelector('.status-dot')!;
      expect(dot.className).not.toContain('danger');
    });
  });

  describe('locators', () => {
    it('hedges an estimated position rather than stating it', async () => {
      const fixture = await render([
        claim({ locator: { kind: 'page', value: 12, precision: 'estimated' } }),
      ]);
      (fixture.nativeElement as HTMLElement).querySelector('button')!.click();
      fixture.detectChanges();
      expect(text(fixture)).toContain('about page 12');
    });

    it('uses the unit the document actually has', async () => {
      const fixture = await render([
        claim({ locator: { kind: 'paragraph', value: 34, precision: 'measured' } }),
      ]);
      (fixture.nativeElement as HTMLElement).querySelector('button')!.click();
      fixture.detectChanges();
      // A Word document has paragraphs; claiming a page would be invented.
      expect(text(fixture)).toContain('paragraph 34');
      expect(text(fixture)).not.toContain('page 34');
    });

    it('does not invent a position when there is none', async () => {
      const fixture = await render([
        claim({ state: 'unverified', locator: null }),
      ]);
      (fixture.nativeElement as HTMLElement).querySelector('button')!.click();
      fixture.detectChanges();
      expect(text(fixture)).not.toMatch(/page \d/);
    });
  });

  describe('fallbacks', () => {
    it('falls back to the claim label when the backend sends no value', async () => {
      const fixture = await render([claim({ value: undefined })]);
      (fixture.nativeElement as HTMLElement).querySelector('button')!.click();
      fixture.detectChanges();
      expect(text(fixture)).toContain('total_award');
    });

    it('omits the document name when claims span more than one', async () => {
      const fixture = await render([
        claim(),
        claim({ id: 'b', filename: 'budget.pdf' }),
      ]);
      expect(text(fixture)).not.toContain('award.pdf');
    });
  });

  describe('accessibility', () => {
    it('wires the toggle to the panel it controls', async () => {
      const fixture = await render([claim()]);
      const host = fixture.nativeElement as HTMLElement;
      const toggle = host.querySelector('button')!;
      toggle.click();
      fixture.detectChanges();
      const panelId = toggle.getAttribute('aria-controls');
      expect(panelId).toBeTruthy();
      expect(host.querySelector(`#${panelId}`)).toBeTruthy();
    });

    it('does not rely on colour alone — every state carries a dot and words', async () => {
      const states: SourceProvenanceClaim['state'][] = [
        'supported',
        'quote_unsupported',
        'unassessed',
        'unverified',
      ];
      for (const state of states) {
        const fixture = await render([claim({ state })]);
        (fixture.nativeElement as HTMLElement).querySelector('button')!.click();
        fixture.detectChanges();
        const host = fixture.nativeElement as HTMLElement;
        expect(host.querySelectorAll('.status-dot').length).toBeGreaterThan(0);
        expect(text(fixture).trim().length).toBeGreaterThan(20);
      }
    });
  });
});
