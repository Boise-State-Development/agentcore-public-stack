import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import { ReviewDiffComponent } from './review-diff.component';
import { AgentVersionDiff } from '../models/marketplace.model';

/**
 * The review diff (§6.1).
 *
 * Two properties are worth guarding, and neither is about how it looks.
 *
 * **It must not fetch until asked.** The queue is a list of decisions; a diff per row on
 * load would pull every pending agent's full instructions down to render a control nobody
 * opened. That is a behavior the template can silently lose.
 *
 * **"First submission" must not read as "nothing changed".** They are opposite claims —
 * one means "read all of this", the other means "approve it" — and the backend sends a
 * distinct flag precisely so the UI cannot collapse them.
 */
describe('ReviewDiffComponent', () => {
  let mockService: { loadDiff: ReturnType<typeof vi.fn> };

  function diff(overrides: Partial<AgentVersionDiff> = {}): AgentVersionDiff {
    return {
      agentId: 'ast-001',
      publishedVersion: 2,
      pendingVersion: 4,
      firstSubmission: false,
      behaviorChanged: false,
      changes: [],
      instructionsDiff: [],
      ...overrides,
    };
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockService = { loadDiff: vi.fn().mockResolvedValue(diff()) };
    TestBed.configureTestingModule({
      providers: [{ provide: AdminMarketplaceService, useValue: mockService }],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  function render(): ComponentFixture<ReviewDiffComponent> {
    const fixture = TestBed.createComponent(ReviewDiffComponent);
    fixture.componentRef.setInput('agentId', 'ast-001');
    fixture.detectChanges();
    return fixture;
  }

  async function expand(fixture: ComponentFixture<ReviewDiffComponent>): Promise<void> {
    const button = (fixture.nativeElement as HTMLElement).querySelector('button')!;
    button.click();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  function text(fixture: ComponentFixture<unknown>): string {
    return (fixture.nativeElement as HTMLElement).textContent ?? '';
  }

  it('does not fetch anything until it is expanded', () => {
    render();
    expect(mockService.loadDiff).not.toHaveBeenCalled();
  });

  it('fetches on the first expand and shows what changed', async () => {
    mockService.loadDiff.mockResolvedValue(
      diff({ behaviorChanged: true, changes: [{ field: 'instructions', behavior: true }] }),
    );
    const fixture = render();
    await expand(fixture);

    expect(mockService.loadDiff).toHaveBeenCalledWith('ast-001');
    expect(text(fixture)).toContain('Instructions');
  });

  it('does not re-fetch when collapsed and expanded again', async () => {
    const fixture = render();
    await expand(fixture);
    await expand(fixture); // collapse
    await expand(fixture); // re-expand

    // The pending version is immutable, so a second request could only return the same
    // bytes — and a reviewer comparing two rows should not pay for it twice.
    expect(mockService.loadDiff).toHaveBeenCalledTimes(1);
  });

  it('names a first submission rather than reporting no changes', async () => {
    mockService.loadDiff.mockResolvedValue(
      diff({ firstSubmission: true, publishedVersion: undefined, behaviorChanged: true }),
    );
    const fixture = render();
    await expand(fixture);

    expect(text(fixture)).toContain('First submission');
    expect(text(fixture)).not.toContain('Identical to the published version');
  });

  it('says so plainly when a resubmission changed nothing', async () => {
    const fixture = render();
    await expand(fixture);
    expect(text(fixture)).toContain('Identical to the published version 2');
  });

  it('styles a behavior change differently from a presentation one', async () => {
    // The asymmetry is the whole point: a tagline fix should be approvable at a glance and
    // an instruction rewrite should not. If these ever render alike, that is gone.
    mockService.loadDiff.mockResolvedValue(
      diff({
        behaviorChanged: true,
        changes: [
          { field: 'instructions', behavior: true },
          { field: 'tagline', behavior: false },
        ],
      }),
    );
    const fixture = render();
    await expand(fixture);

    const items = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('li'),
    ) as HTMLElement[];
    const behavior = items.find((li) => li.textContent?.trim() === 'Instructions')!;
    const presentation = items.find((li) => li.textContent?.trim() === 'Tagline')!;

    expect(behavior.className).not.toBe(presentation.className);
    expect(behavior.className).toContain('amber');
    expect(presentation.className).not.toContain('amber');
  });

  it('renders the instructions diff with added and removed lines distinguished', async () => {
    mockService.loadDiff.mockResolvedValue(
      diff({
        behaviorChanged: true,
        changes: [{ field: 'instructions', behavior: true }],
        instructionsDiff: ['--- approved', '+++ submitted', '-Be concise.', '+Be thorough.'],
      }),
    );
    const fixture = render();
    await expand(fixture);

    const spans = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('pre span'),
    ) as HTMLElement[];
    const removed = spans.find((s) => s.textContent?.includes('-Be concise.'))!;
    const added = spans.find((s) => s.textContent?.includes('+Be thorough.'))!;

    expect(removed.className).toContain('rose');
    expect(added.className).toContain('emerald');
  });

  it('degrades to a message the reviewer can act on when the fetch fails', async () => {
    mockService.loadDiff.mockRejectedValue(new Error('boom'));
    const fixture = render();
    await expand(fixture);

    // Never a blank panel: the reviewer still has to decide, so tell them the diff is
    // missing and that the agent itself is still reviewable.
    expect(text(fixture)).toContain('Could not load what changed');
  });

  it('shows the server’s own explanation instead of the generic failure', async () => {
    // The pre-snapshot case: retrying can never work, and "could not load" invites exactly
    // that. The backend names the real cause and what to do about it — the reviewer has to
    // see *that*, not a transport-shaped message about a data condition.
    mockService.loadDiff.mockRejectedValue({
      error: { detail: 'This submission predates version snapshots, so there is nothing to compare.' },
    });
    const fixture = render();
    await expand(fixture);

    expect(text(fixture)).toContain('predates version snapshots');
    expect(text(fixture)).not.toContain('Could not load what changed');
  });

  it('falls back to the generic failure when the error carries no detail', async () => {
    // A real transport failure has no `detail`, and "try again" is the right advice there.
    mockService.loadDiff.mockRejectedValue({ error: { detail: '   ' } });
    const fixture = render();
    await expand(fixture);

    expect(text(fixture)).toContain('Could not load what changed');
  });

  it('marks the toggle as expanded for assistive technology', async () => {
    const fixture = render();
    const button = (fixture.nativeElement as HTMLElement).querySelector('button')!;
    expect(button.getAttribute('aria-expanded')).toBe('false');

    await expand(fixture);
    expect(button.getAttribute('aria-expanded')).toBe('true');
    expect(button.getAttribute('aria-controls')).toBe('review-diff-ast-001');
  });
});
