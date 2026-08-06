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

  function create(listing: AgentListingBlock | undefined, part?: 'all' | 'badge' | 'note') {
    const fixture = TestBed.createComponent(ListingStatusComponent);
    fixture.componentRef.setInput('listing', listing);
    if (part) fixture.componentRef.setInput('part', part);
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

  // ── "in review" must not read as "off the shelf" ────────────────────────────────
  it('says the published version stays live while an update is reviewed', () => {
    // Without this the badge lies by omission: an author shipping a fix to a live agent
    // sees "In review" and reasonably concludes their listing came down. It did not —
    // the approved snapshot keeps serving until the update is approved.
    const fixture = create(block({ state: 'in_review', publishedVersion: 2 }));
    expect(fixture.nativeElement.textContent).toContain('stays in the store');
  });

  it('says the same while a live listing is being revised', () => {
    // Requesting changes on a published listing deliberately does not unpublish it.
    const fixture = create(block({ state: 'changes_requested', publishedVersion: 2 }));
    expect(fixture.nativeElement.textContent).toContain('still in the store');
  });

  it('claims nothing about a first submission that has never been published', () => {
    const fixture = create(block({ state: 'in_review' }));
    expect(fixture.nativeElement.textContent).not.toContain('store');
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

  // ── `part`, for a layout that needs the two halves in different places ──────────
  describe('part', () => {
    const returned = () =>
      block({ state: 'changes_requested', reviewNote: 'Tighten the tagline.' });

    it('renders both halves by default', () => {
      const text = create(returned()).nativeElement.textContent;
      expect(text).toContain('Changes requested');
      expect(text).toContain('Tighten the tagline.');
    });

    it('splits into a badge and a note that reassemble into the whole', () => {
      // My Agents' list view puts the badge inline beside the name and the note on its
      // own line below. Between them they must lose nothing.
      const badge = create(returned(), 'badge').nativeElement.textContent;
      expect(badge).toContain('Changes requested');
      expect(badge).not.toContain('Tighten the tagline.');

      const note = create(returned(), 'note').nativeElement.textContent;
      expect(note).not.toContain('Changes requested');
      expect(note).toContain('The reviewer asked for changes');
      expect(note).toContain('Tighten the tagline.');
    });

    it('renders nothing for a note part with no note, so a row gains no empty gap', () => {
      const fixture = create(block({ state: 'published' }), 'note');
      expect(fixture.nativeElement.textContent.trim()).toBe('');
    });
  });
});
