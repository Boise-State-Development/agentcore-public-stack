import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { signal } from '@angular/core';
import { ActivatedRoute, Router, provideRouter } from '@angular/router';
import { of } from 'rxjs';

import { SubmissionReviewPage } from './submission-review.page';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import { AdminSubmissionReview } from '../models/marketplace.model';

/**
 * The page that exists because a reviewer could not read what they were approving.
 *
 * The two things asserted here are the two the backend cannot: that the content actually
 * *renders* (instructions especially — the whole point of the endpoint), and that the
 * decision bar offers all three answers on a pending submission and none of them on a
 * listing whose decision is not this page's to make.
 *
 * DI tokens rather than vi.mock, per project convention — a shared worker pool makes
 * module mocks leak across specs.
 */
describe('SubmissionReviewPage', () => {
  let mockService: {
    error: ReturnType<typeof signal<string | null>>;
    loadSubmission: ReturnType<typeof vi.fn>;
    loadDiff: ReturnType<typeof vi.fn>;
    review: ReturnType<typeof vi.fn>;
  };
  let mockDialog: { open: ReturnType<typeof vi.fn> };
  let navigate: ReturnType<typeof vi.fn>;

  function submission(overrides: Partial<AdminSubmissionReview> = {}): AdminSubmissionReview {
    return {
      agentId: 'ast-001',
      name: 'Policy Lookup',
      description: 'Find and cite university policy',
      tagline: 'Policy, cited',
      instructions: 'Answer only from the policy manual, and always cite the section.',
      starters: ['What is the drop deadline?'],
      ownerName: 'Ada Author',
      category: 'Administration',
      categoryLabel: 'Administration',
      state: 'in_review',
      capabilities: [{ label: 'Web search', kind: 'tool' }],
      modelLabel: 'Claude Opus',
      reviewVersion: 4,
      snapshotUnavailable: false,
      reachability: 'everyone',
      ...overrides,
    } as AdminSubmissionReview;
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    navigate = vi.fn().mockResolvedValue(true);
    mockService = {
      error: signal<string | null>(null),
      loadSubmission: vi.fn().mockResolvedValue(submission()),
      loadDiff: vi.fn().mockResolvedValue({
        agentId: 'ast-001',
        firstSubmission: true,
        behaviorChanged: false,
        changes: [],
        instructionsDiff: [],
      }),
      review: vi.fn().mockResolvedValue(undefined),
    };
    mockDialog = { open: vi.fn().mockReturnValue({ closed: of('Because.') }) };
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: AdminMarketplaceService, useValue: mockService },
        { provide: Dialog, useValue: mockDialog },
        { provide: Router, useValue: { navigate } },
        {
          provide: ActivatedRoute,
          useValue: { snapshot: { paramMap: { get: () => 'ast-001' } } },
        },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  async function render(): Promise<ComponentFixture<SubmissionReviewPage>> {
    const fixture = TestBed.createComponent(SubmissionReviewPage);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  function text(fixture: ComponentFixture<unknown>): string {
    return (fixture.nativeElement as HTMLElement).textContent ?? '';
  }

  function button(fixture: ComponentFixture<unknown>, label: string): HTMLButtonElement {
    return Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('button')).find(
      (b) => b.textContent?.includes(label),
    ) as HTMLButtonElement;
  }

  it('shows the instructions — the thing the reviewer could not see before', async () => {
    const fixture = await render();
    expect(text(fixture)).toContain('Answer only from the policy manual');
  });

  it('names what the agent binds, and its model', async () => {
    const fixture = await render();
    const rendered = text(fixture);
    expect(rendered).toContain('Web search');
    expect(rendered).toContain('Claude Opus');
  });

  it('says so when the agent binds nothing, rather than rendering an empty list', async () => {
    mockService.loadSubmission.mockResolvedValue(submission({ capabilities: [] }));
    const fixture = await render();
    expect(text(fixture)).toMatch(/instructions alone/i);
  });

  it('offers all three decisions on a pending submission', async () => {
    const fixture = await render();
    expect(button(fixture, 'Approve')).toBeTruthy();
    expect(button(fixture, 'Request changes')).toBeTruthy();
    expect(button(fixture, 'Decline')).toBeTruthy();
  });

  it('offers no decision on a withdrawal request — that is the queue’s question', async () => {
    // Answering a withdrawal with the submission verbs re-publishes over the author's
    // request without ever saying so. The page reads it; the queue decides it.
    mockService.loadSubmission.mockResolvedValue(
      submission({ state: 'withdrawal_requested', publishedVersion: 4 }),
    );
    const fixture = await render();
    expect(button(fixture, 'Approve')).toBeFalsy();
    expect(button(fixture, 'Decline')).toBeFalsy();
  });

  it('offers no decision on a listing already decided', async () => {
    mockService.loadSubmission.mockResolvedValue(submission({ state: 'published' }));
    const fixture = await render();
    expect(button(fixture, 'Approve')).toBeFalsy();
    expect(text(fixture)).toMatch(/no decision waiting/i);
  });

  it('declines through review with the reason and returns to the queue', async () => {
    const fixture = await render();
    button(fixture, 'Decline').click();
    await fixture.whenStable();

    expect(mockService.review).toHaveBeenCalledWith('ast-001', {
      decision: 'reject',
      note: 'Because.',
    });
    expect(navigate).toHaveBeenCalledWith(['/admin/marketplace/review']);
  });

  it('records nothing when the decline dialog is dismissed', async () => {
    mockDialog.open.mockReturnValue({ closed: of(undefined) });
    const fixture = await render();
    button(fixture, 'Decline').click();
    await fixture.whenStable();

    expect(mockService.review).not.toHaveBeenCalled();
  });

  it('approves without a dialog', async () => {
    const fixture = await render();
    button(fixture, 'Approve').click();
    await fixture.whenStable();

    expect(mockService.review).toHaveBeenCalledWith('ast-001', { decision: 'approve' });
  });

  it('warns when the content is not frozen', async () => {
    // A submission predating version snapshots: the reviewer may still read it, but the
    // author can change it under them, and the page must not imply otherwise.
    mockService.loadSubmission.mockResolvedValue(
      submission({ snapshotUnavailable: true, reviewVersion: undefined }),
    );
    const fixture = await render();
    expect(text(fixture)).toMatch(/predates version snapshots/i);
  });

  it('warns that a private agent will 404 for everyone but its author', async () => {
    mockService.loadSubmission.mockResolvedValue(submission({ reachability: 'owner_only' }));
    const fixture = await render();
    expect(text(fixture)).toMatch(/only the author can open this/i);
  });

  it('surfaces the backend message when the read fails', async () => {
    mockService.loadSubmission.mockRejectedValue({
      error: { detail: 'This agent has no marketplace listing to review.' },
    });
    const fixture = await render();
    expect(text(fixture)).toContain('no marketplace listing to review');
  });

  it('keeps the reviewer on the page when a decision fails', async () => {
    mockService.review.mockRejectedValue({ error: { detail: 'Visibility is now Private.' } });
    const fixture = await render();
    button(fixture, 'Approve').click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(navigate).not.toHaveBeenCalled();
    expect(text(fixture)).toContain('Visibility is now Private.');
    // Re-enabled, or the reviewer is stranded with a dead action bar.
    expect(button(fixture, 'Approve').hasAttribute('disabled')).toBe(false);
  });
});
