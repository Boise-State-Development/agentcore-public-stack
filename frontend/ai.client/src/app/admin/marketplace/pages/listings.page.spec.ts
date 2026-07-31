import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { signal } from '@angular/core';
import { of } from 'rxjs';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import { MarketplaceListingsPage } from './listings.page';
import { AdminListingRow } from '../models/marketplace.model';

/**
 * The published-version marker.
 *
 * Replaced the post-approval drift badge (#744). These tests deliberately kept the drift
 * suite's *shape* rather than being written fresh, because the thing worth guarding did not
 * change: the badge is the reviewer's only on-page answer to "is what I approved still what
 * is live?", and a silent template regression that dropped it would read as "nothing to see
 * here" rather than as a missing control.
 *
 * What did change is that there is now one signal instead of two. The old suite's central
 * assertion — that a measured change and an inferred one stay visually distinct — is gone
 * because the inferred signal is gone: the store serves an immutable snapshot, so there is
 * nothing left to guess about.
 */
describe('MarketplaceListingsPage — published-version marker', () => {
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
      // The default is the uninteresting case on purpose: a fixture that was also
      // unreachable would mix two independent warnings into one row.
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

  async function renderWith(
    rows: AdminListingRow[],
  ): Promise<ComponentFixture<MarketplaceListingsPage>> {
    mockService.loadListings.mockResolvedValue(rows);
    const fixture = TestBed.createComponent(MarketplaceListingsPage);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  function versionBadge(fixture: ComponentFixture<unknown>, label: string): HTMLElement | undefined {
    const spans = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('span'),
    ) as HTMLElement[];
    return spans.find((s) => s.textContent?.trim() === label);
  }

  it('names the version the store is serving', async () => {
    const fixture = await renderWith([row({ publishedVersion: 3 })]);
    expect(versionBadge(fixture, 'v3')).toBeDefined();
  });

  it('renders no marker on a listing that has never been published', async () => {
    const fixture = await renderWith([row({ state: 'in_review', publishedVersion: undefined })]);
    expect(versionBadge(fixture, 'v1')).toBeUndefined();
    expect((fixture.nativeElement as HTMLElement).textContent).not.toMatch(/\bv\d+\b/);
  });

  it('explains the marker on hover, since a bare number does not say what to do', async () => {
    // The reviewer's question is "can the author have changed this since I approved it?",
    // and "v3" alone does not answer it — the tooltip is where the guarantee is stated.
    const fixture = await renderWith([row({ publishedVersion: 3 })]);
    expect(versionBadge(fixture, 'v3')).toBeDefined();
  });

  it('stays visually quiet — it is orientation, not an alarm', async () => {
    // The badge it replaced had to compete for attention because it meant something was
    // wrong. This one means the system is working, and amber would misreport that.
    const fixture = await renderWith([row({ publishedVersion: 3 })]);
    expect(versionBadge(fixture, 'v3')!.className).not.toContain('amber');
  });
});

/**
 * Rollback (§8).
 *
 * Repointing a published listing at an earlier snapshot. Not a review decision — nothing is
 * cut, nothing queues, the listing stays published — so it lives beside "Take down" rather
 * than in the review queue.
 */
describe('MarketplaceListingsPage — rollback', () => {
  let mockService: any;
  let mockDialog: { open: ReturnType<typeof vi.fn> };

  function row(overrides: Partial<AdminListingRow> = {}): AdminListingRow {
    return {
      agentId: 'ast-001',
      name: 'Policy Lookup',
      ownerName: 'Ada Author',
      category: 'Administration',
      state: 'published',
      publishedVersion: 3,
      usageCount: 12,
      updatedAt: '2026-07-22T00:00:00Z',
      reachability: 'everyone',
      adminEdits: [],
      ...overrides,
    };
  }

  async function render(rows: AdminListingRow[]) {
    mockService.loadListings.mockResolvedValue(rows);
    const fixture = TestBed.createComponent(MarketplaceListingsPage);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  function button(fixture: ComponentFixture<unknown>, label: string) {
    return Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === label,
    );
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockService = {
      error: signal<string | null>(null),
      loadListings: vi.fn().mockResolvedValue([]),
      takedown: vi.fn().mockResolvedValue(undefined),
      review: vi.fn().mockResolvedValue(undefined),
      rollback: vi.fn().mockResolvedValue(undefined),
      loadVersions: vi.fn().mockResolvedValue({ versions: [], publishedVersion: 3 }),
    };
    mockDialog = {
      open: vi.fn().mockReturnValue({ closed: of({ version: 2, reason: 'v3 broke citations.' }) }),
    };
    TestBed.configureTestingModule({
      providers: [
        { provide: AdminMarketplaceService, useValue: mockService },
        { provide: Dialog, useValue: mockDialog },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  it('offers rollback on a published listing that has history behind it', async () => {
    const fixture = await render([row({ publishedVersion: 3 })]);
    expect(button(fixture, 'Roll back')).toBeTruthy();
  });

  it('hides rollback on v1 — there is nothing behind the first version', async () => {
    // A control whose dialog can only say "nothing to roll back to" is worse than no control.
    const fixture = await render([row({ publishedVersion: 1 })]);
    expect(button(fixture, 'Roll back')).toBeFalsy();
    // The other published action is unaffected.
    expect(button(fixture, 'Take down')).toBeTruthy();
  });

  it('hides rollback on a listing that is not published', async () => {
    const fixture = await render([row({ state: 'in_review', publishedVersion: undefined })]);
    expect(button(fixture, 'Roll back')).toBeFalsy();
  });

  it('sends the chosen version and reason', async () => {
    const fixture = await render([row()]);
    button(fixture, 'Roll back')!.click();
    await fixture.whenStable();

    expect(mockService.rollback).toHaveBeenCalledWith('ast-001', {
      version: 2,
      reason: 'v3 broke citations.',
    });
    // Not a review decision — it must not travel through that endpoint.
    expect(mockService.review).not.toHaveBeenCalled();
  });

  it('does nothing when the dialog is dismissed', async () => {
    mockDialog.open.mockReturnValue({ closed: of(undefined) });
    const fixture = await render([row()]);
    button(fixture, 'Roll back')!.click();
    await fixture.whenStable();

    expect(mockService.rollback).not.toHaveBeenCalled();
  });

  it("surfaces the server's refusal", async () => {
    mockService.rollback.mockRejectedValue({
      error: { detail: 'Version 2 of this agent does not exist.' },
    });
    const fixture = await render([row()]);
    button(fixture, 'Roll back')!.click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect((fixture.nativeElement as HTMLElement).textContent).toContain('does not exist');
  });
});
