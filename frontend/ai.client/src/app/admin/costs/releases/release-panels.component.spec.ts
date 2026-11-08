import { afterEach, describe, expect, it } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Type } from '@angular/core';
import { ReleaseKpiCardsComponent } from './release-kpi-cards.component';
import { ReleaseCompareHeaderComponent } from './release-compare-header.component';
import { ReleaseQualityPanelComponent } from './release-quality-panel.component';
import { ReleaseToolsTableComponent } from './release-tools-table.component';
import { ReleaseVersionStripComponent } from './release-version-strip.component';
import { unitChips } from './release-ledger.util';
import { readoutFixture, versionsFixture } from '../../../../testing/release-ledger.fixtures';

function render<T>(component: Type<T>, inputs: Record<string, unknown>) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({});
  const fixture = TestBed.createComponent(component);
  for (const [name, value] of Object.entries(inputs)) fixture.componentRef.setInput(name, value);
  fixture.detectChanges();
  return fixture;
}

const text = (el: Element | null): string => (el?.textContent ?? '').replace(/\s+/g, ' ').trim();

afterEach(() => TestBed.resetTestingModule());

describe('ReleaseKpiCardsComponent', () => {
  it('puts cost per turn first as baseline → candidate (Δ), broken out per model', () => {
    const fixture = render(ReleaseKpiCardsComponent, { readout: readoutFixture() });
    const el: HTMLElement = fixture.nativeElement;
    const headline = text(el.querySelector('section > div'));
    expect(headline).toContain('Cost per turn');
    expect(headline).toContain('$0.00865 → to $0.0123');
    expect(headline).toContain('+41.7%');
    expect(headline).toContain('n 956 / 117 turns');
    expect(text(el.querySelector('table'))).toContain('us.anthropic.claude-haiku-5-5');
  });

  it('renders a delta under its floor as "—" with its n, never as a number', () => {
    const fixture = render(ReleaseKpiCardsComponent, { readout: readoutFixture() });
    const card = fixture.nativeElement.querySelector('[data-kpi="emptyAnswerShare"]');
    expect(text(card.querySelector('[data-testid="kpi-delta"]'))).toBe('Δ —, not comparable');
    expect(text(card.querySelector('[data-testid="kpi-n"]'))).toBe('n 900 / 14 turns with an outcome, under floor');
    expect(text(card)).toContain('0.40% → to —');
  });

  it('writes the direction out, so colour is never the only signal', () => {
    const fixture = render(ReleaseKpiCardsComponent, { readout: readoutFixture() });
    const card = fixture.nativeElement.querySelector('[data-kpi="cacheEfficiency"]');
    expect(text(card.querySelector('[data-testid="kpi-delta"]'))).toBe('Δ −3.3%, worse');
  });

  it('shows first-token percentiles from the readout’s latency block', () => {
    const fixture = render(ReleaseKpiCardsComponent, { readout: readoutFixture() });
    expect(text(fixture.nativeElement)).toContain('2,030 ms → 2,714 ms');
  });
});

describe('ReleaseCompareHeaderComponent', () => {
  const chips = unitChips(versionsFixture(), 'build');

  it('shows the mix-shift warning and every caveat, and no KPI value', () => {
    const fixture = render(ReleaseCompareHeaderComponent, { readout: readoutFixture(), chips });
    const el: HTMLElement = fixture.nativeElement;
    expect(text(el.querySelector('[data-testid="mix-shift"]'))).toContain('us.anthropic.claude-haiku-5-5: 45.0% → 92.0% of calls');
    const caveats = el.querySelectorAll('[data-testid="caveats"] li');
    expect(caveats.length).toBe(2);
    expect(text(caveats[0])).toContain('no_full_day');
    expect(text(el)).not.toContain('$0.00865');
  });

  it('omits the warning when the mix held', () => {
    const readout = readoutFixture({ mixShift: { warn: false, models: {}, activeUsers: [4, 4] } });
    const fixture = render(ReleaseCompareHeaderComponent, { readout, chips });
    expect(fixture.nativeElement.querySelector('[data-testid="mix-shift"]')).toBeNull();
  });

  it('offers only units before the candidate as a baseline', () => {
    const fixture = render(ReleaseCompareHeaderComponent, { readout: readoutFixture(), chips });
    const options = [...fixture.nativeElement.querySelectorAll('#release-baseline option')].map(o => (o as HTMLOptionElement).value);
    expect(options).toEqual(['', '1.33.0#f200a6a3514248d3', '1.33.0#git-0123456789ab']);
  });
});

describe('ReleaseQualityPanelComponent', () => {
  it('shows the eval table verbatim and thumbs as counts below the floor', () => {
    const fixture = render(ReleaseQualityPanelComponent, { readout: readoutFixture() });
    const el: HTMLElement = fixture.nativeElement;
    const cells = [...el.querySelectorAll('tbody tr')].map(r => [...r.children].map(c => text(c)));
    expect(cells).toContainEqual(['tool_selection', '9', '9', '9', '0', '0', '1.000', 'pass']);
    expect(text(el)).toContain('No eval run published for this unit.');
    expect(text(el)).toContain('11 pass, 0 fail, 6 skip, 1 observed');
    const thumbs = text(el.querySelector('[data-testid="thumbs"]'));
    expect(thumbs).toContain('Baseline: 3 up, 1 down, below floor (n=4)');
    expect(thumbs).not.toMatch(/down rate/);
  });

  it('has no overall quality score anywhere (D6)', () => {
    const fixture = render(ReleaseQualityPanelComponent, { readout: readoutFixture() });
    expect(text(fixture.nativeElement).toLowerCase()).not.toMatch(/score/);
  });
});

describe('ReleaseToolsTableComponent', () => {
  it('sorts by estimated dollars, prints pairs, and flags rows whose payload was not measured', () => {
    const fixture = render(ReleaseToolsTableComponent, { tools: readoutFixture().tools, notMeasured: ['awaitingUser'] });
    const rows = fixture.nativeElement.querySelectorAll('tbody tr');
    expect(text(rows[0])).toContain('browse_web');
    expect(text(rows[0])).toContain('4.0% → 9.0%');
    expect(text(rows[1])).toContain('fetch_url_content (payload not measured)');
    expect(text(rows[1])).toContain('310');
    expect(text(fixture.nativeElement)).toContain('Not recorded by the tool census yet: awaiting-user share.');
  });
});

describe('ReleaseVersionStripComponent', () => {
  it('marks the candidate as pressed and labels the baseline', () => {
    const fixture = render(ReleaseVersionStripComponent, {
      chips: unitChips(versionsFixture(), 'build'),
      unit: 'build',
      candidateKey: '1.33.0#d5ead33e05f3e229',
      baselineKey: '1.33.0#f200a6a3514248d3',
    });
    const buttons = [...fixture.nativeElement.querySelectorAll('button')] as HTMLButtonElement[];
    const pressed = buttons.filter(b => b.getAttribute('aria-pressed') === 'true');
    expect(pressed.length).toBe(1);
    expect(pressed[0].getAttribute('aria-label')).toContain('1.33.0#d5ead33e05f3e229, candidate');
    expect(text(buttons[1])).toContain('Baseline');
    expect(buttons[0].getAttribute('aria-label')).toContain('attributed by the deploy-time map');
  });

  it('emits the chosen chip as the candidate', () => {
    const fixture = render(ReleaseVersionStripComponent, { chips: unitChips(versionsFixture(), 'build'), unit: 'build' });
    const emitted: string[] = [];
    fixture.componentInstance.candidateChange.subscribe(k => emitted.push(k));
    (fixture.nativeElement.querySelector('button') as HTMLButtonElement).click();
    expect(emitted).toEqual(['1.33.0#git-0123456789ab']);
  });
});
