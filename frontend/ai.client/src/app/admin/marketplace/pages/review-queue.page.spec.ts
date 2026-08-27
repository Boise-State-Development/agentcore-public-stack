import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { signal } from '@angular/core';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import { ReviewQueuePage } from './review-queue.page';
import { AdminListingRow } from '../models/marketplace.model';
import { of } from 'rxjs';
import { provideRouter } from '@angular/router';
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
        // The agent name is a RouterLink into the submission review page, so the row
        // cannot render without an ActivatedRoute.
        provideRouter([]),
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

/**
 * Withdrawal requests in the review queue (§5.1).
 *
 * They share the queue with submissions, and before this they shared its *controls* too —
 * so an author asking for their listing to come down was answered with Approve/Request
 * changes. "Approve" then routed to `review`, which walks `withdrawal_requested →
 * published`: the request was silently declined and the admin was told nothing.
 *
 * These assert the row is legible as a different question and that it reaches the endpoint
 * built for it.
 */
describe('ReviewQueuePage — withdrawal requests', () => {
  let mockService: any;
  let mockDialog: { open: ReturnType<typeof vi.fn> };

  function withdrawalRow(overrides: Partial<AdminListingRow> = {}): AdminListingRow {
    return {
      agentId: 'ast-002',
      name: 'Policy Lookup',
      ownerName: 'Ada Author',
      category: 'Administration',
      state: 'withdrawal_requested',
      usageCount: 4,
      submittedAt: '2026-07-01T00:00:00Z',
      withdrawalRequestedAt: '2026-07-22T00:00:00Z',
      updatedAt: '2026-07-22T00:00:00Z',
      reachability: 'everyone',
      adminEdits: [],
      ...overrides,
    };
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockService = {
      error: signal<string | null>(null),
      loadSubmissions: vi.fn().mockResolvedValue([withdrawalRow()]),
      review: vi.fn().mockResolvedValue(undefined),
      decideWithdrawal: vi.fn().mockResolvedValue(undefined),
    };
    mockDialog = { open: vi.fn().mockReturnValue({ closed: of('') }) };
    TestBed.configureTestingModule({
      providers: [
        { provide: AdminMarketplaceService, useValue: mockService },
        { provide: Dialog, useValue: mockDialog },
        provideRouter([]),
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  async function render(): Promise<ComponentFixture<ReviewQueuePage>> {
    const fixture = TestBed.createComponent(ReviewQueuePage);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  function button(fixture: ComponentFixture<unknown>, label: string): HTMLButtonElement {
    return Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('button')).find(
      (b) => b.textContent?.includes(label),
    ) as HTMLButtonElement;
  }

  it('labels the row as a withdrawal request rather than a submission', async () => {
    const fixture = await render();
    const rendered = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(rendered).toContain('Withdrawal requested');
    expect(rendered).toMatch(/asked to pull it/i);
    // The submission framing must be gone, or the admin answers the wrong question.
    expect(rendered).not.toMatch(/· submitted/i);
  });

  it('offers grant and decline, never the submission verbs', async () => {
    const fixture = await render();
    expect(button(fixture, 'Take it down')).toBeTruthy();
    expect(button(fixture, 'Keep published')).toBeTruthy();
    expect(button(fixture, 'Approve')).toBeFalsy();
    expect(button(fixture, 'Request changes')).toBeFalsy();
  });

  it('grants through the withdrawal endpoint, not through review', async () => {
    const fixture = await render();
    button(fixture, 'Take it down').click();
    await fixture.whenStable();

    expect(mockService.decideWithdrawal).toHaveBeenCalledWith('ast-002', {
      decision: 'grant',
      note: undefined,
    });
    // The bug this replaced: `review` with `approve` re-published over the request.
    expect(mockService.review).not.toHaveBeenCalled();
  });

  it('declines with the admin note attached', async () => {
    mockDialog.open.mockReturnValue({ closed: of('Still needed for fall registration.') });
    const fixture = await render();
    button(fixture, 'Keep published').click();
    await fixture.whenStable();

    expect(mockService.decideWithdrawal).toHaveBeenCalledWith('ast-002', {
      decision: 'decline',
      note: 'Still needed for fall registration.',
    });
  });

  it('does nothing when the confirmation is dismissed', async () => {
    mockDialog.open.mockReturnValue({ closed: of(undefined) });
    const fixture = await render();
    button(fixture, 'Take it down').click();
    await fixture.whenStable();

    expect(mockService.decideWithdrawal).not.toHaveBeenCalled();
  });

  it('tells the admin the listing is still live while they decide', async () => {
    // The one fact that makes the decision make sense: a request is not a removal, so
    // declining restores nothing and granting is what actually takes it down.
    const fixture = await render();
    expect((fixture.nativeElement as HTMLElement).textContent).toMatch(/still live in the store/i);
  });
});

/**
 * Declining from the queue (the third review decision).
 *
 * Before it, an admin who judged a submission not a fit had to approve it or say "fix
 * this" — promising a review they did not intend to give, and seeing the same submission
 * again every round. These assert it reaches `review` (not the withdrawal endpoint, which
 * answers the opposite question) and that it never appears on a withdrawal row.
 */
describe('ReviewQueuePage — declining a submission', () => {
  let mockService: any;
  let mockDialog: { open: ReturnType<typeof vi.fn> };

  function submissionRow(overrides: Partial<AdminListingRow> = {}): AdminListingRow {
    return {
      agentId: 'ast-003',
      name: 'Policy Lookup',
      ownerName: 'Ada Author',
      category: 'Administration',
      state: 'in_review',
      usageCount: 0,
      submittedAt: '2026-07-22T00:00:00Z',
      updatedAt: '2026-07-22T00:00:00Z',
      reachability: 'everyone',
      adminEdits: [],
      ...overrides,
    };
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockService = {
      error: signal<string | null>(null),
      loadSubmissions: vi.fn().mockResolvedValue([submissionRow()]),
      review: vi.fn().mockResolvedValue(undefined),
      decideWithdrawal: vi.fn().mockResolvedValue(undefined),
    };
    mockDialog = { open: vi.fn().mockReturnValue({ closed: of('Duplicates the Registrar agent.') }) };
    TestBed.configureTestingModule({
      providers: [
        { provide: AdminMarketplaceService, useValue: mockService },
        { provide: Dialog, useValue: mockDialog },
        provideRouter([]),
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  async function render(): Promise<ComponentFixture<ReviewQueuePage>> {
    const fixture = TestBed.createComponent(ReviewQueuePage);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  function button(fixture: ComponentFixture<unknown>, label: string): HTMLButtonElement {
    return Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('button')).find(
      (b) => b.textContent?.includes(label),
    ) as HTMLButtonElement;
  }

  it('offers Decline alongside the other two decisions', async () => {
    const fixture = await render();
    expect(button(fixture, 'Decline')).toBeTruthy();
    expect(button(fixture, 'Request changes')).toBeTruthy();
    expect(button(fixture, 'Approve')).toBeTruthy();
  });

  it('declines through review with the reason attached', async () => {
    const fixture = await render();
    button(fixture, 'Decline').click();
    await fixture.whenStable();

    expect(mockService.review).toHaveBeenCalledWith('ast-003', {
      decision: 'reject',
      note: 'Duplicates the Registrar agent.',
    });
    // Never the withdrawal endpoint: that one answers "may this come out?", and routing a
    // decline through it would take down a listing that was never up.
    expect(mockService.decideWithdrawal).not.toHaveBeenCalled();
  });

  it('records nothing when the reason dialog is dismissed', async () => {
    mockDialog.open.mockReturnValue({ closed: of(undefined) });
    const fixture = await render();
    button(fixture, 'Decline').click();
    await fixture.whenStable();

    expect(mockService.review).not.toHaveBeenCalled();
  });

  it('links the agent name to its full review', async () => {
    const fixture = await render();
    const link = (fixture.nativeElement as HTMLElement).querySelector('a[href*="ast-003"]');
    expect(link).toBeTruthy();
    expect(link?.getAttribute('href')).toContain('/admin/marketplace/review/ast-003');
  });
});
