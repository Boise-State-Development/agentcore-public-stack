import { describe, it, expect } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';

import { ChatContainerComponent } from './chat-container.component';
import { Agent } from '../../../agents/models/agent.model';
import { ListingState } from '../../../agents/models/store.model';

/**
 * The foot-of-conversation feedback link is offered for **published** marketplace agents
 * and nothing else (D15.3 — you may report what the store offered you). The backend gate
 * is the real one; this keeps us from rendering a link whose only outcome is a 400.
 *
 * The other half is what it reads *from*. It deliberately does not use the launch card's
 * `listed` flag, which falls back to the Assistant shape with `listed: false` — an
 * affordance that silently vanished whenever the `/agents` load lost a race would be hard
 * to notice and harder to explain.
 */
describe('ChatContainerComponent — the feedback link gate', () => {
  function agent(listingState: ListingState | null): Agent {
    return {
      agentId: 'ast-001',
      ownerName: 'Ada Author',
      name: 'Policy Lookup',
      description: 'Find and cite university policy',
      bindings: [],
      visibility: 'PUBLIC',
      tags: [],
      starters: [],
      usageCount: 0,
      status: 'COMPLETE',
      createdAt: '2026-07-01T00:00:00Z',
      updatedAt: '2026-07-01T00:00:00Z',
      ...(listingState
        ? { listing: { state: listingState, category: 'Administration' } }
        : {}),
    } as Agent;
  }

  function feedbackAgentFor(value: Agent | null): { id: string; name: string } | null {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    });

    const fixture = TestBed.createComponent(ChatContainerComponent);
    fixture.componentRef.setInput('messages', []);
    fixture.componentRef.setInput('agent', value);
    return fixture.componentInstance['feedbackAgent']();
  }

  it('offers feedback on a published agent', () => {
    expect(feedbackAgentFor(agent('published'))).toEqual({ id: 'ast-001', name: 'Policy Lookup' });
  });

  it.each<ListingState>(['private', 'in_review', 'changes_requested', 'taken_down'])(
    'offers nothing for a %s listing',
    (state) => {
      expect(feedbackAgentFor(agent(state))).toBeNull();
    },
  );

  it('offers nothing for an agent that was never listed', () => {
    expect(feedbackAgentFor(agent(null))).toBeNull();
  });

  it('offers nothing for plain chat', () => {
    // No Agent means we do not *know* this is a store agent — the only honest reason to
    // withhold the link.
    expect(feedbackAgentFor(null)).toBeNull();
  });
});
