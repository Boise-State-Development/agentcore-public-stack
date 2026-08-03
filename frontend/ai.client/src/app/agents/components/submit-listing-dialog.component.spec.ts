import { describe, it, expect, beforeEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';

import { SubmitListingDialogComponent } from './submit-listing-dialog.component';
import { AgentListingService } from '../services/agent-listing.service';
import { ListingPreflight, SubmitListingRequest } from '../models/store.model';

/**
 * The dialog owns the *consent* rule, and getting it wrong has two bad shapes: an author
 * blocked from publishing at all (the dead end this control exists to remove), or an
 * agent's visibility widened without the author having said so.
 *
 * DI tokens rather than vi.mock, per project convention — a shared worker pool makes
 * module mocks leak across specs.
 */
describe('SubmitListingDialogComponent — going public', () => {
  let submitted: { agentId: string; request: SubmitListingRequest }[];

  function build(preflight: Partial<ListingPreflight>): SubmitListingDialogComponent {
    submitted = [];
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: DialogRef, useValue: { close: () => undefined } },
        { provide: DIALOG_DATA, useValue: { agentId: 'ast-001', agentName: 'Policy Lookup' } },
        {
          provide: AgentListingService,
          useValue: {
            loadCategories: async () => [{ id: 'Administration', label: 'Administration' }],
            preflight: async (): Promise<ListingPreflight> => ({
              agentId: 'ast-001',
              exposedSkills: [],
              blockReason: null,
              requiresPublic: false,
              reachability: 'everyone',
              ...preflight,
            }),
            submit: async (agentId: string, request: SubmitListingRequest) => {
              submitted.push({ agentId, request });
              return { listing: { state: 'in_review', category: request.category } };
            },
          },
        },
      ],
    });
    return TestBed.createComponent(SubmitListingDialogComponent).componentInstance;
  }

  describe('when the agent is not public yet', () => {
    let component: SubmitListingDialogComponent;

    beforeEach(async () => {
      component = build({ requiresPublic: true, reachability: 'owner_only' });
      await component.ngOnInit();
      component.category.set('Administration');
    });

    it('asks for consent instead of dead-ending the author', () => {
      // The whole point: no blockReason, so the form renders and the author can act here
      // rather than being sent to the agent editor and back.
      expect(component.requiresPublic()).toBe(true);
      expect(component.blockReason()).toBeNull();
    });

    it('starts unticked — going public is a decision, not a default', () => {
      expect(component.makePublic()).toBe(false);
    });

    it('holds Submit until the author consents', () => {
      expect(component.canSubmit()).toBe(false);
      component.makePublic.set(true);
      expect(component.canSubmit()).toBe(true);
    });

    it('sends the consent so the backend widens in the same write', async () => {
      component.makePublic.set(true);
      await component.onSubmit();

      expect(submitted).toHaveLength(1);
      expect(submitted[0].request.makePublic).toBe(true);
    });

    it('says what going public changes from, per starting state', async () => {
      expect(component.makePublicHelp()).toMatch(/only you can open it/i);

      const shared = build({ requiresPublic: true, reachability: 'shared_only' });
      await shared.ngOnInit();
      expect(shared.makePublicHelp()).toMatch(/shared/i);
      expect(shared.makePublicHelp()).not.toBe(component.makePublicHelp());
    });
  });

  describe('when the agent is already public', () => {
    it('asks nothing and does not send a consent it never sought', async () => {
      const component = build({ requiresPublic: false, reachability: 'everyone' });
      await component.ngOnInit();
      component.category.set('Administration');

      expect(component.requiresPublic()).toBe(false);
      expect(component.canSubmit()).toBe(true);

      await component.onSubmit();
      expect(submitted[0].request.makePublic).toBeUndefined();
    });
  });

  describe('when something really does block submission', () => {
    it('stays a dead end — the consent checkbox must not wave it through', async () => {
      const component = build({
        blockReason: 'This agent cannot be published while it is bound to a memory space.',
        requiresPublic: true,
        reachability: 'owner_only',
      });
      await component.ngOnInit();
      component.category.set('Administration');
      component.makePublic.set(true);

      expect(component.canSubmit()).toBe(false);
    });
  });
});

/**
 * Category preselection on a resubmission.
 *
 * The shelf an Agent already sits on is the answer nine times out of ten, and an author
 * resubmitting a Teaching listing should not have to notice that the picker quietly offered
 * them Administration instead — that silently moves a live listing to a different shelf.
 */
describe('SubmitListingDialogComponent — category preselection', () => {
  function build(
    data: Record<string, unknown>,
    categories = [
      { id: 'Administration', label: 'Administration' },
      { id: 'Teaching', label: 'Teaching' },
    ],
  ): SubmitListingDialogComponent {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: DialogRef, useValue: { close: () => undefined } },
        {
          provide: DIALOG_DATA,
          useValue: { agentId: 'ast-001', agentName: 'Policy Lookup', ...data },
        },
        {
          provide: AgentListingService,
          useValue: {
            loadCategories: async () => categories,
            preflight: async (): Promise<ListingPreflight> => ({
              agentId: 'ast-001',
              exposedSkills: [],
              blockReason: null,
              requiresPublic: false,
              reachability: 'everyone',
            }),
            submit: async () => ({ listing: { state: 'in_review' } }),
          },
        },
      ],
    });
    return TestBed.createComponent(SubmitListingDialogComponent).componentInstance;
  }

  it("keeps the listing's current shelf on a resubmission", async () => {
    const component = build({ listing: { state: 'private', category: 'Teaching' } });
    await component.ngOnInit();

    expect(component.category()).toBe('Teaching');
  });

  it('leaves a first submission unselected rather than guessing', async () => {
    // An author who never chose a shelf must choose one — defaulting to whichever category
    // sorts first would file new listings under it by accident.
    const component = build({});
    await component.ngOnInit();

    expect(component.category()).toBe('');
  });

  it('clears a shelf that has since been closed to new listings', async () => {
    const component = build({ listing: { state: 'private', category: 'Retired' } });
    await component.ngOnInit();

    expect(component.category()).toBe('');
  });

  // ── an update is not a first appearance ────────────────────────────────────────
  it('knows an update from a submission by what is published, not by state', async () => {
    // A listing an admin sent back for changes keeps serving while the author revises it,
    // so the state name alone gets this wrong. Promising "an admin reviews this before it
    // appears in the store" to someone whose agent is already in the store reads as though
    // submitting had taken it down.
    const update = build({
      listing: { state: 'changes_requested', category: 'Teaching', publishedVersion: 2 },
    });
    expect(update.isUpdate()).toBe(true);

    const neverPublished = build({ listing: { state: 'changes_requested', category: 'Teaching' } });
    expect(neverPublished.isUpdate()).toBe(false);
    expect(neverPublished.isResubmission()).toBe(true);
  });

  /**
   * ⚠️ Asserts the **rendered** select, not `category()`.
   *
   * Every other test here reads the signal, and the signal was never the broken half — which
   * is how a live desync survived a describe block named for exactly this behaviour. The
   * picker showed `Administration` on a `Student Support` listing (verified in dev), because
   * `categories()` resolves after the first render and a `[value]` on the select had nothing
   * to match yet. The author saw a shelf they never chose.
   *
   * The ordering below is the test, and it is subtler than "options load late". The whole
   * form sits behind `@if (loading())`, so the select never renders with an empty list —
   * select and options render together, in one pass, and Angular applies an element's own
   * bindings before it creates that element's children. A `[value]` on the select therefore
   * lands while the select still has no options, and the browser drops it. Binding on the
   * options cannot lose that race, because the option *is* the thing being created.
   *
   * So: first render (loading, no form), flush the load, second render (the real one).
   */
  it('shows the author the shelf their listing is already on', async () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: DialogRef, useValue: { close: () => undefined } },
        {
          provide: DIALOG_DATA,
          useValue: {
            agentId: 'ast-001',
            agentName: 'Policy Lookup',
            listing: { state: 'published', category: 'Student Support', publishedVersion: 2 },
          },
        },
        {
          provide: AgentListingService,
          useValue: {
            // Deliberately not first in the list: the bug selects index 0, so a category
            // that sorts first would let it pass.
            loadCategories: async () => [
              { id: 'Administration', label: 'Administration' },
              { id: 'Teaching', label: 'Teaching' },
              { id: 'Student Support', label: 'Student Support' },
            ],
            preflight: async (): Promise<ListingPreflight> => ({
              agentId: 'ast-001',
              exposedSkills: [],
              blockReason: null,
              requiresPublic: false,
              reachability: 'everyone',
            }),
            submit: async () => ({ listing: { state: 'in_review' } }),
          },
        },
      ],
    });

    const fixture = TestBed.createComponent(SubmitListingDialogComponent);
    fixture.detectChanges();
    // A macrotask, not `whenStable` — the component's loads are bare promises, and the
    // form stays behind `loading()` until they settle.
    await new Promise((resolve) => setTimeout(resolve, 0));
    fixture.detectChanges();

    const select: HTMLSelectElement =
      fixture.nativeElement.querySelector('#listing-category');
    expect(select.value).toBe('Student Support');
    expect(select.options[select.selectedIndex].text.trim()).toBe('Student Support');
    expect(fixture.componentInstance.category()).toBe('Student Support');
  });
});
