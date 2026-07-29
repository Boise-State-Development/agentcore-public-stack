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
