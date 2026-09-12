import { TestBed, ComponentFixture } from '@angular/core/testing';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { provideRouter, ActivatedRoute } from '@angular/router';
import { ReactiveFormsModule } from '@angular/forms';
import { Component, input, output } from '@angular/core';
import { AgentFormPage } from './agent-form.page';
import { AgentPreviewComponent } from './components/agent-preview.component';
import { KnowledgeBaseSectionComponent } from '../../knowledge-base/knowledge-base-section.component';
import { AgentService } from '../services/agent.service';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { ThemeService } from '../../components/topnav/components/theme-toggle/theme.service';
import { ToastService } from '../../services/toast/toast.service';

/**
 * `max_tokens` is deliberately not an author-facing knob — see the
 * `AUTHOR_HIDDEN_PARAMS` block comment in `agent-form.page.ts`. These tests pin
 * both halves of that: the model catalog can declare it and the Designer still
 * won't render it, and an agent saved with one before the filter existed drops
 * it on hydrate rather than carrying an invisible override.
 */

@Component({ selector: 'app-agent-preview', template: '' })
class StubPreviewComponent {
  agentId = input<string | null>(null);
  name = input('');
  description = input('');
  emoji = input('');
  starters = input<string[]>([]);
  modelId = input<string | null>(null);
  isDirty = input(false);
  saving = input(false);
  canSave = input(false);
  save = output<void>();
  openFull = output<void>();
}

@Component({ selector: 'app-knowledge-base-section', template: '' })
class StubKnowledgeBaseComponent {
  entityId = input<string | null>(null);
  userPermission = input('owner');
  permissionResolved = input(false);
  createDraft = input<unknown>(null);
}

async function settle(fixture: ComponentFixture<AgentFormPage>): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  fixture.detectChanges();
}

/** Mirrors the live dev record for Claude Sonnet 5: max_tokens + effort. */
const MODEL_WITH_MAX_TOKENS = {
  kind: 'model',
  ref: 'us.anthropic.claude-sonnet-5',
  label: 'Claude Sonnet 5',
  description: 'Anthropic',
  meta: {
    provider: 'bedrock',
    supportedParams: {
      params: {
        max_tokens: { supported: true, locked: false, min: 1, max: 128_000, default: 128_000 },
        effort: {
          supported: true,
          locked: false,
          allowed: ['low', 'medium', 'high', 'xhigh'],
          default: 'medium',
        },
      },
    },
  },
};

/** A model whose only editable param is the one we hide — section must collapse. */
const MODEL_WITH_ONLY_MAX_TOKENS = {
  ...MODEL_WITH_MAX_TOKENS,
  ref: 'us.anthropic.claude-maxtokens-only',
  label: 'Max Tokens Only',
  meta: {
    provider: 'bedrock',
    supportedParams: {
      params: {
        max_tokens: { supported: true, locked: false, min: 1, max: 8_192, default: 4_096 },
      },
    },
  },
};

/** An admin-locked max_tokens must not surface in the read-only list either. */
const MODEL_WITH_LOCKED_MAX_TOKENS = {
  ...MODEL_WITH_MAX_TOKENS,
  ref: 'us.anthropic.claude-locked',
  label: 'Locked Max Tokens',
  meta: {
    provider: 'bedrock',
    supportedParams: {
      params: {
        max_tokens: { supported: true, locked: true, min: 1, max: 4_096, default: 4_096 },
      },
    },
  },
};

function setup(options: { agent?: unknown } = {}) {
  const mockAgentService = {
    loadBindable: vi.fn(async (kind: string) =>
      kind === 'model'
        ? [MODEL_WITH_MAX_TOKENS, MODEL_WITH_ONLY_MAX_TOKENS, MODEL_WITH_LOCKED_MAX_TOKENS]
        : [],
    ),
    createAgent: vi.fn().mockResolvedValue({ agentId: 'agt-1' }),
    updateAgent: vi.fn().mockResolvedValue({ agentId: 'agt-1' }),
    getAgent: vi.fn().mockResolvedValue(options.agent),
  };
  const mockToast = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };

  TestBed.resetTestingModule();
  vi.clearAllMocks();
  Element.prototype.scrollIntoView = vi.fn();

  TestBed.configureTestingModule({
    imports: [ReactiveFormsModule],
    providers: [
      provideRouter([{ path: 'agents', children: [] }]),
      { provide: AgentService, useValue: mockAgentService },
      { provide: ToastService, useValue: mockToast },
      { provide: SidenavService, useValue: { hide: vi.fn(), show: vi.fn() } },
      { provide: ThemeService, useValue: { isDark: () => false } },
      {
        provide: ActivatedRoute,
        useValue: {
          snapshot: { paramMap: { get: () => (options.agent ? 'agt-1' : null) } },
        },
      },
    ],
  });

  TestBed.overrideComponent(AgentFormPage, {
    remove: { imports: [AgentPreviewComponent, KnowledgeBaseSectionComponent] },
    add: { imports: [StubPreviewComponent, StubKnowledgeBaseComponent] },
  });

  return { mockAgentService, mockToast };
}

describe('AgentFormPage — max_tokens is not author-facing', () => {
  let component: AgentFormPage;
  let fixture: ComponentFixture<AgentFormPage>;

  async function render(options: { agent?: unknown } = {}) {
    const mocks = setup(options);
    fixture = TestBed.createComponent(AgentFormPage);
    component = fixture.componentInstance;
    fixture.detectChanges();
    await fixture.whenStable();
    await settle(fixture);
    return mocks;
  }

  beforeEach(async () => {
    await render();
  });

  it('omits max_tokens from the editable params even when the model declares it', () => {
    component.selectModel('us.anthropic.claude-sonnet-5');
    fixture.detectChanges();

    const keys = component.numberParams().map((p) => p.key);
    expect(keys).not.toContain('max_tokens');
  });

  it('still renders the other params the model declares', () => {
    component.selectModel('us.anthropic.claude-sonnet-5');
    fixture.detectChanges();

    expect(component.enumParams().map((p) => p.key)).toContain('effort');
    expect(component.hasParamControls()).toBe(true);
  });

  it('omits an admin-locked max_tokens from the read-only list too', () => {
    component.selectModel('us.anthropic.claude-locked');
    fixture.detectChanges();

    expect(component.lockedParams().map((p) => p.key)).not.toContain('max_tokens');
  });

  it('hides the whole Parameters section when max_tokens was the only param', () => {
    component.selectModel('us.anthropic.claude-maxtokens-only');
    fixture.detectChanges();

    expect(component.hasParamControls()).toBe(false);
    expect(fixture.nativeElement.textContent).not.toContain('Max tokens');
  });

  it('never renders a "Max tokens" control for a model that declares one', () => {
    component.selectModel('us.anthropic.claude-sonnet-5');
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('#param-max_tokens')).toBeNull();
    expect(fixture.nativeElement.textContent).not.toContain('Max tokens');
  });

  it('drops a previously-saved max_tokens on hydrate, keeping the rest', async () => {
    await render({
      agent: {
        agentId: 'agt-1',
        name: 'Legacy Agent',
        description: 'Saved before max_tokens was hidden',
        instructions: 'You are helpful.',
        visibility: 'private',
        tags: [],
        modelConfig: {
          modelId: 'us.anthropic.claude-sonnet-5',
          params: { max_tokens: 2000, effort: 'high' },
        },
        bindings: [],
      },
    });

    expect(component.modelParams()).toEqual({ effort: 'high' });
  });
});
