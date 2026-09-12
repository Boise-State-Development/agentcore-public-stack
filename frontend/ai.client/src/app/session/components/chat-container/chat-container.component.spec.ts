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

/**
 * Step 4 of `docs/specs/customize-surface.md`: with the composer's settings
 * drawer gone, the assistant indicator becomes the only place a user can learn
 * that this conversation's model, tools and skills are fixed by the Agent — and
 * therefore why the preferences they set in Customize are being ignored here.
 *
 * Derived from the Agent record rather than from `ToolService.agentLocked()` &
 * friends, which live on root singletons and outlive the view that set them.
 */
describe('ChatContainerComponent — agent governance for the indicator', () => {
  function agentWith(overrides: Partial<Agent>): Agent {
    return {
      agentId: 'ast-001',
      ownerName: 'Ada Author',
      name: 'Rubric Builder',
      description: 'Build a rubric',
      bindings: [],
      visibility: 'PRIVATE',
      tags: [],
      starters: [],
      usageCount: 0,
      status: 'COMPLETE',
      createdAt: '2026-07-01T00:00:00Z',
      updatedAt: '2026-07-01T00:00:00Z',
      ...overrides,
    } as Agent;
  }

  function governanceFor(value: Agent | null) {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    });
    const fixture = TestBed.createComponent(ChatContainerComponent);
    fixture.componentRef.setInput('messages', []);
    fixture.componentRef.setInput('agent', value);
    return fixture.componentInstance['agentGovernance']();
  }

  it('claims nothing without an agent — which is what the previews get', () => {
    // The Designer preview and the marketplace test-drive render this component
    // without passing `[agent]`, and neither applies the picker locks. Silence
    // is the correct answer there, and it falls out of the derivation for free.
    expect(governanceFor(null)).toBeNull();
  });

  it('claims nothing for an agent that binds nothing', () => {
    expect(governanceFor(agentWith({ bindings: [] }))).toBeNull();
  });

  it('counts tool and skill bindings separately', () => {
    const value = governanceFor(
      agentWith({
        bindings: [
          { kind: 'tool', ref: 'web_search' },
          { kind: 'tool', ref: 'calculator' },
          { kind: 'skill', ref: 'sk_apa' },
        ],
      } as Partial<Agent>),
    );
    expect(value).toEqual({ modelName: null, toolCount: 2, skillCount: 1 });
  });

  it('falls back to the raw model id when the catalog has not loaded', () => {
    // Naming the model badly beats dropping the row that says one is pinned.
    const value = governanceFor(
      agentWith({ modelConfig: { modelId: 'us.anthropic.claude-sonnet-5' } } as Partial<Agent>),
    );
    expect(value?.modelName).toBe('us.anthropic.claude-sonnet-5');
  });

  it('reports a pinned model even when no tools or skills are bound', () => {
    const value = governanceFor(
      agentWith({ modelConfig: { modelId: 'm-1' }, bindings: [] } as Partial<Agent>),
    );
    expect(value).toEqual({ modelName: 'm-1', toolCount: null, skillCount: null });
  });
});
