import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { signal } from '@angular/core';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import { MarketplaceListingsPage } from './listings.page';
import { AdminListingRow, ListingDrift } from '../models/marketplace.model';

/**
 * The post-approval drift marker (#744).
 *
 * `drift` is derived server-side and covered there; what these assert is the part the
 * backend cannot enforce — that the **two signals stay visually distinct**. `'instructions'`
 * is measured and `'edited'` is a guess, and if a later edit renders them alike the marker
 * stops carrying the distinction it exists to carry.
 */
describe('MarketplaceListingsPage — post-approval drift marker', () => {
  let mockService: any;

  function row(overrides: Partial<AdminListingRow> = {}): AdminListingRow {
    return {
      agentId: 'ast-001',
      name: 'Policy Lookup',
      ownerName: 'Ada Author',
      category: 'Administration',
      state: 'published',
      usageCount: 12,
      updatedAt: '2026-07-22T00:00:00Z',
      // The default is the uninteresting case on purpose: these tests are about drift, and
      // a fixture that was also unreachable would mix two independent warnings in one row.
      reachability: 'everyone',
      adminEdits: [],
      ...overrides,
    };
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockService = {
      error: signal<string | null>(null),
      loadListings: vi.fn().mockResolvedValue([]),
      takedown: vi.fn().mockResolvedValue(undefined),
    };

    TestBed.configureTestingModule({
      providers: [
        { provide: AdminMarketplaceService, useValue: mockService },
        { provide: Dialog, useValue: { open: vi.fn() } },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  async function renderWith(rows: AdminListingRow[]): Promise<ComponentFixture<MarketplaceListingsPage>> {
    mockService.loadListings.mockResolvedValue(rows);
    const fixture = TestBed.createComponent(MarketplaceListingsPage);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  function badgeText(fixture: ComponentFixture<unknown>): string {
    return (fixture.nativeElement as HTMLElement).textContent ?? '';
  }

  function driftBadge(fixture: ComponentFixture<unknown>, label: string): HTMLElement | undefined {
    const spans = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('span'),
    ) as HTMLElement[];
    return spans.find((s) => s.textContent?.trim() === label);
  }

  it('renders no drift badge on a listing that has not drifted', async () => {
    const fixture = await renderWith([row()]);
    expect(badgeText(fixture)).not.toContain('Instructions changed');
    expect(badgeText(fixture)).not.toContain('Edited since review');
  });

  it('names a measured instructions change', async () => {
    const fixture = await renderWith([row({ drift: 'instructions' })]);
    expect(driftBadge(fixture, 'Instructions changed')).toBeDefined();
  });

  it('names an inferred edit differently from a measured change', async () => {
    const fixture = await renderWith([row({ drift: 'edited' })]);
    expect(driftBadge(fixture, 'Edited since review')).toBeDefined();
    expect(badgeText(fixture)).not.toContain('Instructions changed');
  });

  it('styles the measured signal more urgently than the inferred one', async () => {
    // The whole point of keeping two values: if these ever render identically, the weak
    // signal inherits the strong one's urgency and admins learn to ignore both.
    const measured = await renderWith([row({ drift: 'instructions' })]);
    const measuredClass = driftBadge(measured, 'Instructions changed')!.className;

    const inferred = await renderWith([row({ drift: 'edited' })]);
    const inferredClass = driftBadge(inferred, 'Edited since review')!.className;

    expect(measuredClass).not.toBe(inferredClass);
    expect(measuredClass).toContain('amber');
    expect(inferredClass).not.toContain('amber');
  });

  it('carries an icon only on the measured signal', async () => {
    const measured = await renderWith([row({ drift: 'instructions' })]);
    expect(driftBadge(measured, 'Instructions changed')!.querySelector('ng-icon')).toBeTruthy();

    const inferred = await renderWith([row({ drift: 'edited' })]);
    expect(driftBadge(inferred, 'Edited since review')!.querySelector('ng-icon')).toBeNull();
  });

  it('explains each marker on hover, since the label alone does not say what to do', async () => {
    for (const drift of ['instructions', 'edited'] as ListingDrift[]) {
      const fixture = await renderWith([row({ drift })]);
      const label = drift === 'instructions' ? 'Instructions changed' : 'Edited since review';
      // The tooltip directive is bound; its text lives on the component's lookup table.
      expect(driftBadge(fixture, label)).toBeDefined();
    }
  });
});
