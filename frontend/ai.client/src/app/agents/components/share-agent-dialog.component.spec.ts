import { describe, it, expect, beforeEach, vi } from 'vitest';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';

import { ShareAgentDialogComponent, ShareAgentDialogData } from './share-agent-dialog.component';
import { AssistantService } from '../../assistants/services/assistant.service';
import { UserApiService } from '../../users/services/user-api.service';
import { ConfigService } from '../../services/config.service';
import { AgentService } from '../services/agent.service';
import { AgentListingService } from '../services/agent-listing.service';
import { ShareEntry } from '../../assistants/models/assistant.model';
import { AgentListingBlock } from '../models/store.model';

/** Network-bound doubles. None of the behaviour under test reaches them. */
function baseProviders(overrides: {
  visibility: 'PRIVATE' | 'PUBLIC' | 'SHARED';
  updateAssistant?: () => Promise<void>;
}) {
  const updateAssistant = overrides.updateAssistant ?? (async () => undefined);
  return [
    provideRouter([]),
    provideHttpClient(),
    provideHttpClientTesting(),
    // ConfigService is pulled in transitively by AssistantApiService; stub the one
    // signal it exposes so the component constructs cleanly.
    { provide: ConfigService, useValue: { appApiUrl: () => 'http://test' } },
    {
      provide: AssistantService,
      useValue: {
        getAssistantShares: async () => [],
        shareAssistant: async () => undefined,
        unshareAssistant: async () => undefined,
        updateSharePermission: async () => undefined,
        updateAssistant,
      },
    },
    {
      provide: AgentService,
      useValue: {
        getAgent: async () => ({ agentId: 'ast-test', visibility: overrides.visibility }),
      },
    },
    {
      provide: AgentListingService,
      useValue: {
        available: signal<boolean | null>(null),
        loadCategories: vi.fn().mockResolvedValue([]),
        withdraw: vi.fn(),
      },
    },
    {
      provide: UserApiService,
      useValue: { searchUsers: () => ({ pipe: () => ({ subscribe: () => ({}) }) }) },
    },
    { provide: DialogRef, useValue: { close: () => undefined } },
    {
      provide: DIALOG_DATA,
      useValue: {
        agent: {
          assistantId: 'ast-test',
          name: 'Test',
          visibility: overrides.visibility,
          userPermission: 'owner',
        },
      } satisfies ShareAgentDialogData,
    },
    ShareAgentDialogComponent,
  ];
}

/**
 * Three things this dialog owns that are worth pinning down.
 *
 * `computeDeltas` is the diff between "what was loaded" and "what the user pressed Save
 * with", and it decides which endpoints fire (POST share, DELETE unshare, PATCH update).
 * Getting it wrong issues spurious writes.
 *
 * `typedEmails` decides whether the single add field offers to add what was typed as an
 * address. A false positive invites a share with `jo`; a partial accept on a pasted run
 * silently drops people the user believes they invited.
 *
 * The save path's refusal to narrow a PUBLIC agent is the one that costs real money to
 * get wrong — see the class doc.
 *
 * DI tokens rather than vi.mock (project convention) so this spec cannot leak state into
 * others through the shared worker pool.
 */
describe('ShareAgentDialogComponent', () => {
  let component: ShareAgentDialogComponent;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ providers: baseProviders({ visibility: 'SHARED' }) });
    component = TestBed.inject(ShareAgentDialogComponent);
  });

  describe('computeDeltas', () => {
    it('detects pure additions', () => {
      const initial: ShareEntry[] = [];
      const next: ShareEntry[] = [{ email: 'a@x.com', permission: 'viewer' }];

      const result = (component as any).computeDeltas(initial, next);

      expect(result.adds).toEqual([{ email: 'a@x.com', permission: 'viewer' }]);
      expect(result.removes).toEqual([]);
      expect(result.permissionChanges).toEqual([]);
    });

    it('detects pure removals', () => {
      const initial: ShareEntry[] = [{ email: 'a@x.com', permission: 'viewer' }];
      const next: ShareEntry[] = [];

      const result = (component as any).computeDeltas(initial, next);

      expect(result.adds).toEqual([]);
      expect(result.removes).toEqual(['a@x.com']);
      expect(result.permissionChanges).toEqual([]);
    });

    it('detects a permission upgrade on an already-shared email', () => {
      const initial: ShareEntry[] = [{ email: 'a@x.com', permission: 'viewer' }];
      const next: ShareEntry[] = [{ email: 'a@x.com', permission: 'editor' }];

      const result = (component as any).computeDeltas(initial, next);

      expect(result.adds).toEqual([]);
      expect(result.removes).toEqual([]);
      expect(result.permissionChanges).toEqual([{ email: 'a@x.com', permission: 'editor' }]);
    });

    it('emits no deltas when nothing changed', () => {
      const entries: ShareEntry[] = [
        { email: 'a@x.com', permission: 'viewer' },
        { email: 'b@x.com', permission: 'editor' },
      ];

      const result = (component as any).computeDeltas(
        entries,
        entries.map((entry) => ({ ...entry })),
      );

      expect(result.adds).toEqual([]);
      expect(result.removes).toEqual([]);
      expect(result.permissionChanges).toEqual([]);
    });

    it('handles mixed adds, removes, and permission changes in one save', () => {
      const initial: ShareEntry[] = [
        { email: 'keep@x.com', permission: 'viewer' },
        { email: 'upgrade@x.com', permission: 'viewer' },
        { email: 'drop@x.com', permission: 'editor' },
      ];
      const next: ShareEntry[] = [
        { email: 'keep@x.com', permission: 'viewer' },
        { email: 'upgrade@x.com', permission: 'editor' },
        { email: 'new@x.com', permission: 'viewer' },
      ];

      const result = (component as any).computeDeltas(initial, next);

      expect(result.adds).toEqual([{ email: 'new@x.com', permission: 'viewer' }]);
      expect(result.removes).toEqual(['drop@x.com']);
      expect(result.permissionChanges).toEqual([{ email: 'upgrade@x.com', permission: 'editor' }]);
    });
  });

  describe('typedEmails — one field, no mode toggle', () => {
    /** Set the query directly, bypassing the debounced directory search. */
    function type(value: string): void {
      (component as any).query.set(value);
    }

    it('offers nothing for a partial name', () => {
      type('jo');
      expect((component as any).typedEmails()).toEqual([]);
    });

    it('offers a single typed address, normalised', () => {
      type('  Jo@Example.com ');
      expect((component as any).typedEmails()).toEqual(['jo@example.com']);
    });

    it('offers a comma-separated run as one add', () => {
      type('a@x.com, b@x.com');
      expect((component as any).typedEmails()).toEqual(['a@x.com', 'b@x.com']);
    });

    it('offers nothing when any address in the run is malformed', () => {
      // All-or-nothing: a partial accept reads as "added" while the dropped people never
      // hear about it.
      type('a@x.com, not-an-email, b@x.com');
      expect((component as any).typedEmails()).toEqual([]);
    });

    it('drops an address already on the list', () => {
      (component as any).shares.set([{ email: 'a@x.com', permission: 'viewer' }]);
      type('a@x.com');
      expect((component as any).typedEmails()).toEqual([]);
    });
  });

  describe('save', () => {
    it('never narrows a PUBLIC agent to SHARED', async () => {
      const updateAssistant = vi.fn().mockResolvedValue(undefined);
      TestBed.resetTestingModule();
      TestBed.configureTestingModule({
        providers: baseProviders({ visibility: 'PUBLIC', updateAssistant }),
      });
      const published = TestBed.inject(ShareAgentDialogComponent);

      // A published agent with people on the list. Deriving visibility from the list here
      // would PUT SHARED over PUBLIC and leave the store tile serving to people who then
      // cannot open it.
      (published as any).shares.set([{ email: 'a@x.com', permission: 'viewer' }]);
      await (published as any).onSave();

      expect(updateAssistant).not.toHaveBeenCalled();
    });

    it('widens PRIVATE to SHARED once someone is on the list', async () => {
      const updateAssistant = vi.fn().mockResolvedValue(undefined);
      TestBed.resetTestingModule();
      TestBed.configureTestingModule({
        providers: baseProviders({ visibility: 'PRIVATE', updateAssistant }),
      });
      const isolated = TestBed.inject(ShareAgentDialogComponent);

      (isolated as any).shares.set([{ email: 'a@x.com', permission: 'viewer' }]);
      await (isolated as any).onSave();

      expect(updateAssistant).toHaveBeenCalledWith('ast-test', { visibility: 'SHARED' });
    });
  });

  /**
   * The marketplace controls, which have to mirror the backend transition table exactly —
   * a button the machine refuses is a dead click, and a missing button is a dead end.
   *
   * The dead end is why these exist: `published` was absent from `canSubmit`, so an author
   * whose agent was live had no way to ship a fix. Their edits land on the draft and reach
   * nobody until a new version is approved, and the only routes out were to request
   * withdrawal (taking their own listing down over a typo) or to wait for an admin.
   */
  describe('marketplace controls', () => {
    function withListing(listing: Partial<AgentListingBlock> | undefined) {
      (component as any).listing.set(
        listing
          ? ({
              state: 'published',
              category: 'Teaching',
              publisherId: 'user-user-001',
              ...listing,
            } as AgentListingBlock)
          : undefined,
      );
      return component as any;
    }

    it('offers an update on a published listing', () => {
      const c = withListing({ state: 'published', publishedVersion: 2 });
      expect(c.canSubmit()).toBe(true);
      expect(c.submitLabel()).toBe('Submit an update');
    });

    it('calls a first submission what it is', () => {
      const c = withListing(undefined);
      expect(c.canSubmit()).toBe(true);
      expect(c.submitLabel()).toBe('Submit to marketplace');
    });

    it('offers nothing to submit while a submission is already pending', () => {
      expect(withListing({ state: 'in_review' }).canSubmit()).toBe(false);
    });

    it('offers nothing to submit while a withdrawal is awaiting a decision', () => {
      // `withdrawal_requested → in_review` is not an edge; the admin decides first.
      expect(withListing({ state: 'withdrawal_requested', publishedVersion: 2 }).canSubmit()).toBe(
        false,
      );
    });

    it('calls taking back a pending update a cancellation, not a withdrawal', () => {
      // ⚠️ The author is taking back an edit, and the listing goes on serving the version
      // it always was. "Withdraw submission" reads as though the live listing were at
      // stake, and the confirm copy behind it promised an admin review that never happens.
      const c = withListing({
        state: 'in_review',
        publishedVersion: 2,
        submittedFrom: 'published',
      });
      expect(c.hasPendingUpdate()).toBe(true);
      expect(c.withdrawLabel()).toBe('Cancel update');
    });

    it('still calls a first submission a withdrawal', () => {
      const c = withListing({ state: 'in_review' });
      expect(c.hasPendingUpdate()).toBe(false);
      expect(c.withdrawLabel()).toBe('Withdraw submission');
    });

    it('does not read a stale origin left behind by an earlier cycle', () => {
      // `submittedFrom` is meaningful only while `in_review`; approval does not clear it.
      const c = withListing({
        state: 'published',
        publishedVersion: 3,
        submittedFrom: 'published',
      });
      expect(c.hasPendingUpdate()).toBe(false);
      expect(c.withdrawLabel()).toBe('Request withdrawal');
    });
  });

  /**
   * `canWithdraw` mirrors the backend transition table, and it drifted from it.
   *
   * `taken_down` was excluded with a comment saying the machine allowed no author edge out
   * of it — true when written. But delete accepts only `private`, and its refusal told the
   * author to "take it back to private first", so the author of a taken-down agent was sent
   * to a button this component did not render. Backend edge and button have to land together
   * or the fix is invisible from the only seat that matters.
   */
  describe('canWithdraw', () => {
    it('offers an exit from a taken-down listing', () => {
      (component as any).listing.set({ state: 'taken_down' });

      expect((component as any).canWithdraw()).toBe(true);
      // Not "Withdraw" — an admin already pulled it, so there is nothing to withdraw from.
      expect((component as any).withdrawLabel()).toBe('Remove listing');
    });

    it.each(['in_review', 'changes_requested', 'published'] as const)(
      'still offers it from %s',
      (state) => {
        (component as any).listing.set({ state });
        expect((component as any).canWithdraw()).toBe(true);
      },
    );

    it.each(['private', 'withdrawal_requested'] as const)('offers nothing from %s', (state) => {
      // `private` is already the destination. `withdrawal_requested` is a request the author
      // has made and cannot make twice — the backend refuses it with a 400, and offering the
      // button anyway would walk them into that error.
      (component as any).listing.set({ state });
      expect((component as any).canWithdraw()).toBe(false);
    });

    it('offers nothing when the agent was never submitted', () => {
      expect((component as any).canWithdraw()).toBe(false);
    });
  });
});
