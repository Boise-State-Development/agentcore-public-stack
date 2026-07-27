import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { signal } from '@angular/core';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import { ReviewQueuePage } from './review-queue.page';
import { AdminListingRow } from '../models/marketplace.model';
import { ListingReachability } from '../../../agents/models/reachability';

/**
 * The reachability warning on the review queue.
 *
 * The store browse read applies no access check while the detail read does, so approving a
 * PRIVATE or SHARED agent shelves a tile that 404s for everyone it is not shared with.
 * The reviewer is the last person who can notice, and nothing else on the row tells them.
 *
 * These assert the two things the backend cannot: that the warning **renders** for the
 * limited states, and that it stays **advisory** — Approve is never disabled by it.
 */
describe('ReviewQueuePage — reachability warning', () => {
  let mockService: any;

  function row(reachability: ListingReachability): AdminListingRow {
    return {
      agentId: 'ast-001',
      name: 'Policy Lookup',
      ownerName: 'Ada Author',
      category: 'Administration',
      state: 'in_review',
      usageCount: 0,
      updatedAt: '2026-07-22T00:00:00Z',
      reachability,
      adminEdits: [],
    };
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockService = {
      error: signal<string | null>(null),
      loadSubmissions: vi.fn().mockResolvedValue([]),
      review: vi.fn().mockResolvedValue(undefined),
    };
    TestBed.configureTestingModule({
      providers: [
        { provide: AdminMarketplaceService, useValue: mockService },
        { provide: Dialog, useValue: { open: vi.fn() } },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  async function renderWith(rows: AdminListingRow[]): Promise<ComponentFixture<ReviewQueuePage>> {
    mockService.loadSubmissions.mockResolvedValue(rows);
    const fixture = TestBed.createComponent(ReviewQueuePage);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  function text(fixture: ComponentFixture<unknown>): string {
    return (fixture.nativeElement as HTMLElement).textContent ?? '';
  }

  it('says nothing for a public agent', async () => {
    const fixture = await renderWith([row('everyone')]);
    expect(text(fixture)).not.toMatch(/error when they open it|nobody else can use/i);
  });

  it('warns the reviewer that a private agent is unopenable by anyone else', async () => {
    const fixture = await renderWith([row('owner_only')]);
    expect(text(fixture)).toMatch(/only the author can open this/i);
  });

  it('warns on a shared agent too, in its own words', async () => {
    const fixture = await renderWith([row('shared_only')]);
    const rendered = text(fixture);
    expect(rendered).toMatch(/shared/i);
    expect(rendered).toMatch(/get an error/i);
    // Must not be described as author-only — that is a different, more alarming claim.
    expect(rendered).not.toMatch(/only the author can open this/i);
  });

  it('leaves Approve enabled — this is advice, not a gate', async () => {
    // Publishing a SHARED agent to a team is legitimate. A warning that blocked it would
    // make the reviewer's only escape route "change someone else's visibility".
    const fixture = await renderWith([row('owner_only')]);
    const approve = (fixture.nativeElement as HTMLElement).querySelectorAll('button');
    const approveBtn = Array.from(approve).find((b) => b.textContent?.includes('Approve'));
    expect(approveBtn).toBeTruthy();
    expect(approveBtn!.hasAttribute('disabled')).toBe(false);
  });
});
