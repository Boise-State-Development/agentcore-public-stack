import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { OverrideListComponent } from './override-list.component';
import { ConfigService } from '../../../../services/config.service';
import type { QuotaOverride } from '../../models/quota.models';

/**
 * The quota writers emitted `datetime.now(timezone.utc).isoformat() + 'Z'`, i.e.
 * `…+00:00Z`. `DatePipe` THROWS on that string (it is not a soft fallback like
 * `new Date`), and a throw inside a template aborts the change-detection pass part
 * way through the `@for` — every card is created, but the bindings after the
 * throw never update until some later event triggers another pass. That is the
 * "cards appear, data populates slowly / only after a click" symptom.
 */
// Midday UTC so DatePipe (which renders in the runner's local zone) shows
// 7/27/26 anywhere from UTC-11 to UTC+11. At 05:09 UTC every US zone showed 7/26.
const LEGACY = '2026-07-27T12:09:55.853557+00:00Z';

function override(id: string, userId: string): QuotaOverride {
  return {
    overrideId: id,
    userId,
    overrideType: 'custom_limit',
    monthlyCostLimit: 50,
    validFrom: '2026-07-01T00:00:00.000Z',
    validUntil: '2099-01-01T00:00:00.000Z',
    reason: `reason for ${userId}`,
    createdBy: 'admin@example.com',
    createdAt: LEGACY,
    enabled: true,
  };
}

describe('OverrideListComponent', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: signal('http://api.test') } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
    TestBed.resetTestingModule();
  });

  async function renderWith(rows: QuotaOverride[]): Promise<string> {
    const fixture = TestBed.createComponent(OverrideListComponent);
    fixture.autoDetectChanges();
    http.expectOne(r => r.url === 'http://api.test/admin/quota/overrides').flush(rows);
    // Let the awaited HTTP promise settle and the scheduled render run — once.
    // No click, no manual second detectChanges.
    await new Promise(r => setTimeout(r));
    await fixture.whenStable();
    return (fixture.nativeElement as HTMLElement).textContent ?? '';
  }

  it('control: renders cards when createdAt is already valid ISO 8601', async () => {
    const text = await renderWith([
      { ...override('o-1', 'alice@example.com'), createdAt: '2026-07-27T05:09:55.853557Z' },
    ]);
    expect(text).toContain('alice@example.com');
    expect(text).toContain('by admin@example.com');
  });

  it('renders every card in full when createdAt uses the legacy +00:00Z spelling', async () => {
    const text = await renderWith([
      override('o-1', 'alice@example.com'),
      override('o-2', 'bob@example.com'),
    ]);

    expect(text).toContain('alice@example.com');
    expect(text).toContain('bob@example.com');
    expect(text).toContain('reason for bob@example.com');
    expect(text).toContain('by admin@example.com');
    // createdAt is rendered as a real date, not dropped.
    expect(text).toMatch(/7\/27\/26/);
  });
});
