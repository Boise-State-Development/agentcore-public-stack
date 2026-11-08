import { afterEach, describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { computed, signal } from '@angular/core';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { AdminCostsPage } from './admin-costs.page';
import { AdminCostStateService } from './services';
import { ReleaseLedgerHttpService } from './services/release-ledger-http.service';
import { FEATURES } from '../../services/features';
import { readoutFixture, versionsFixture } from '../../../testing/release-ledger.fixtures';

function costStateStub() {
  const systemSummary = signal(null);
  return {
    loading: signal(false),
    loadingTopUsers: signal(false),
    loadingTopSessions: signal(false),
    error: signal<string | null>(null),
    selectedPeriod: signal('2026-11'),
    topUsers: signal([]),
    topUsersCount: computed(() => 0),
    topSessions: signal([]),
    topSessionsTruncated: signal(false),
    trends: signal([]),
    modelUsage: signal([]),
    platformCosts: signal(null),
    totalCost: computed(() => 0),
    activeUsers: computed(() => 0),
    cacheSavings: computed(() => 0),
    systemSummary,
    loadDashboard: vi.fn(async () => undefined),
    loadPlatformCosts: vi.fn(async () => undefined),
  };
}

function ledgerHttpStub() {
  return {
    getVersions: vi.fn(() => of(versionsFixture())),
    getSeries: vi.fn(() => of({ from: '', to: '', by: 'version', unit: 'build', series: [], bands: [], omittedKeys: 0 })),
    getQuality: vi.fn(() => of({ from: '', to: '', unit: 'build', minimumN: 20, units: [], thumbsCoverage: {}, provenanceCoverage: {} })),
    getReadout: vi.fn(() => of(readoutFixture())),
    getTools: vi.fn(() => of({ from: '', to: '', version: null, tools: [], notMeasured: [] })),
    exportNdjson: vi.fn(() => of(new Blob())),
  };
}

function setup(releaseLedger: boolean) {
  TestBed.resetTestingModule();
  const ledger = ledgerHttpStub();
  TestBed.configureTestingModule({
    providers: [
      provideRouter([]),
      { provide: AdminCostStateService, useValue: costStateStub() },
      { provide: ReleaseLedgerHttpService, useValue: ledger },
      { provide: FEATURES, useValue: { releaseLedger } },
    ],
  });
  const fixture = TestBed.createComponent(AdminCostsPage);
  fixture.detectChanges();
  return { fixture, ledger };
}

const tabLabels = (el: HTMLElement) =>
  [...el.querySelectorAll('[role="tab"]')].map(t => (t.textContent ?? '').replace(/\s+/g, ' ').trim());

describe('AdminCostsPage: Releases tab', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('is not offered, and the ledger is never asked, while features.releaseLedger is off', () => {
    const { fixture, ledger } = setup(false);
    const el: HTMLElement = fixture.nativeElement;
    expect(tabLabels(el)).not.toContain('Releases');
    expect([...el.querySelectorAll('select option')].map(o => o.textContent?.trim())).not.toContain('Releases');
    expect(ledger.getVersions).not.toHaveBeenCalled();
  });

  it('is the sixth tab while the flag is on, and loads the ledger only when opened', async () => {
    const { fixture, ledger } = setup(true);
    const el: HTMLElement = fixture.nativeElement;
    expect(tabLabels(el)).toEqual(['Model Usage', 'Cost Trends', 'Platform', 'Top Users', 'Conversations', 'Releases']);
    expect(ledger.getVersions).not.toHaveBeenCalled();

    (el.querySelector('#cost-tab-releases') as HTMLButtonElement).click();
    await vi.waitFor(() => {
      fixture.detectChanges();
      expect(el.querySelector('#cost-panel-releases')?.textContent).toContain('Mix shift');
    });

    expect(ledger.getVersions).toHaveBeenCalledTimes(1);
    const panel = el.querySelector('#cost-panel-releases');
    expect(panel?.getAttribute('aria-labelledby')).toBe('cost-tab-releases');
    expect(panel?.textContent).toContain('Mix shift: read this before any delta');
    expect(panel?.textContent).toContain('Cost per turn');
  });
});
