import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ListingStatusComponent } from './listing-status.component';
import { AgentListingBlock } from '../models/store.model';

describe('ListingStatusComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
  });

  afterEach(() => TestBed.resetTestingModule());

  function create(listing: AgentListingBlock | undefined) {
    const fixture = TestBed.createComponent(ListingStatusComponent);
    fixture.componentRef.setInput('listing', listing);
    fixture.detectChanges();
    return fixture;
  }

  function block(overrides: Partial<AgentListingBlock> = {}): AgentListingBlock {
    return {
      state: 'published',
      category: 'Teaching',
      publisherId: 'user-user-001',
      ...overrides,
    };
  }

  it('renders nothing for an agent that was never submitted', () => {
    const fixture = create(undefined);
    expect(fixture.nativeElement.textContent.trim()).toBe('');
  });

  it('names the state in the same words the reviewer sees', () => {
    const fixture = create(block({ state: 'changes_requested' }));
    expect(fixture.nativeElement.textContent).toContain('Changes requested');
  });

  it('renders the reviewer note inline, so the author never has to ask', () => {
    const fixture = create(
      block({ state: 'changes_requested', reviewNote: 'Tighten the tagline.' }),
    );
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('The reviewer asked for changes');
    expect(text).toContain('Tighten the tagline.');
  });

  it('does not attribute an in-review note to a reviewer', () => {
    // Submission stores the author's own note in the same field; calling it the
    // reviewer's would be a lie the author can see through.
    const fixture = create(block({ state: 'in_review', reviewNote: 'For the CTL team' }));
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('Note on this submission');
    expect(text).not.toContain('reviewer asked');
  });

  it('does not attribute a withdrawn listing note either way', () => {
    // `private` is reached from both `in_review` (author's note survives) and
    // `changes_requested` (the reviewer's does), so the heading cannot claim a voice.
    const fixture = create(block({ state: 'private', reviewNote: 'Verification run.' }));
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('Note on this listing');
    expect(text).not.toContain('reviewer');
  });

  it('surfaces the D13 admin-edit trail with a date', () => {
    const fixture = create(
      block({ adminEdits: [{ field: 'category', at: '2026-07-24T10:00:00Z', by: 'Dana' }] }),
    );
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('An admin updated the category');
    expect(text).toContain('Jul 24');
  });

  it('tolerates the backend timestamp that carries both an offset and a Z', () => {
    const fixture = create(
      block({ adminEdits: [{ field: 'tagline', at: '2026-07-24T10:00:00+00:00Z', by: 'Dana' }] }),
    );
    expect(fixture.nativeElement.textContent).toContain('Jul 24');
  });

  it('drops the date rather than printing "Invalid Date"', () => {
    const fixture = create(block({ adminEdits: [{ field: 'icon', at: 'not-a-date', by: 'Dana' }] }));
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('An admin updated the icon');
    expect(text).not.toContain('Invalid');
  });

  it('collapses an append-only log to the latest edit per field, newest first', () => {
    const component = create(
      block({
        adminEdits: [
          { field: 'tagline', at: '2026-07-20T10:00:00Z', by: 'Dana' },
          { field: 'tagline', at: '2026-07-22T10:00:00Z', by: 'Dana' },
          { field: 'category', at: '2026-07-24T10:00:00Z', by: 'Sam' },
        ],
      }),
    ).componentInstance;

    expect(component.edits().map((e) => e.field)).toEqual(['category', 'tagline']);
    expect(component.edits()[1].at).toBe('2026-07-22T10:00:00Z');
  });
});
