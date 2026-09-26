import { TestBed, ComponentFixture } from '@angular/core/testing';
import { describe, it, expect, vi } from 'vitest';
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
import { ToolService } from '../../services/tool/tool.service';

/**
 * §7 of docs/specs/model-retirement.md — the Agent Designer's half.
 *
 * A model being retired can be kept or removed, never newly chosen. Unlike a
 * tool, a retired model stays in the catalog as a tombstone forever, so an
 * unselected one is hidden rather than shown disabled; the agent's own model
 * is always shown, with a notice saying what happens and what to do.
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


function model(ref: string, label: string, meta: Record<string, unknown> = {}) {
  return { kind: 'model', ref, label, description: 'Anthropic', meta: { provider: 'bedrock', ...meta } };
}

const SONNET_5 = model('sonnet-5', 'Claude Sonnet 5', { status: 'active' });
const SONNET_46 = model('sonnet-4-6', 'Claude Sonnet 4.6', {
  status: 'deprecated',
  replacedBy: 'sonnet-5',
  replacedByName: 'Claude Sonnet 5',
  retiresOn: '2026-10-31',
});
const OPUS_41 = model('opus-4-1', 'Claude Opus 4.1', {
  status: 'retired',
  replacedBy: 'sonnet-5',
  replacedByName: 'Claude Sonnet 5',
});
const CLAUDE_3 = model('claude-3', 'Claude 3', { status: 'retired' });
/** An older backend omits `meta.status` entirely. */
const LEGACY = model('haiku-4-5', 'Claude Haiku 4.5');

async function mount(modelId: string | null): Promise<{ page: AgentFormPage; fixture: ComponentFixture<AgentFormPage> }> {
  TestBed.resetTestingModule();
  vi.clearAllMocks();
  Element.prototype.scrollIntoView = vi.fn();

  const agentService = {
    loadBindable: vi
      .fn()
      .mockImplementation((kind: string) =>
        Promise.resolve(kind === 'model' ? [SONNET_5, SONNET_46, OPUS_41, CLAUDE_3, LEGACY] : []),
      ),
    getAgent: vi.fn().mockResolvedValue({
      agentId: 'agt-1',
      name: 'Rubric Builder',
      description: 'Builds rubrics',
      instructions: 'You build rubrics.',
      visibility: 'PRIVATE',
      userPermission: 'owner',
      bindings: [],
      modelConfig: modelId ? { modelId } : null,
    }),
    createAgent: vi.fn().mockResolvedValue({ agentId: 'agt-1' }),
    updateAgent: vi.fn().mockResolvedValue({ agentId: 'agt-1' }),
  };

  TestBed.configureTestingModule({
    imports: [ReactiveFormsModule],
    providers: [
      provideRouter([{ path: 'agents', children: [] }]),
      {
        provide: ToolService,
        useValue: { initialized: () => true, tools: () => [], loadTools: vi.fn() },
      },
      { provide: AgentService, useValue: agentService },
      {
        provide: ToastService,
        useValue: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
      },
      { provide: SidenavService, useValue: { hide: vi.fn(), show: vi.fn() } },
      { provide: ThemeService, useValue: { isDark: () => false } },
      { provide: ActivatedRoute, useValue: { snapshot: { paramMap: { get: () => 'agt-1' } } } },
    ],
  });

  TestBed.overrideComponent(AgentFormPage, {
    remove: { imports: [AgentPreviewComponent, KnowledgeBaseSectionComponent] },
    add: { imports: [StubPreviewComponent, StubKnowledgeBaseComponent] },
  });

  const fixture = TestBed.createComponent(AgentFormPage);
  fixture.detectChanges();
  await fixture.whenStable();
  await new Promise((resolve) => setTimeout(resolve, 0));
  fixture.detectChanges();
  return { page: fixture.componentInstance, fixture };
}

const refs = (page: AgentFormPage) => page.visibleModels().map((m) => m.ref);

describe('AgentFormPage — retiring models', () => {
  it('offers only active models to an agent that uses none of the retiring ones', async () => {
    const { page } = await mount('sonnet-5');
    expect(refs(page)).toEqual(['sonnet-5', 'haiku-4-5']);
    expect(page.retiringModelNotice()).toBeNull();
  });

  it('keeps showing the agent’s own deprecated model, badged, with a notice', async () => {
    const { page, fixture } = await mount('sonnet-4-6');
    expect(refs(page)).toContain('sonnet-4-6');

    const notice = page.retiringModelNotice()!;
    expect(notice.lead).toBe('Claude Sonnet 4.6 is being retired.');
    expect(notice.detail).toContain('Claude Sonnet 5 answers in its place');
    expect(notice.action).toContain('keeps working for now');
    expect(notice.action).toContain('resubmit');

    const card = [...fixture.nativeElement.querySelectorAll('button[aria-pressed]')].find((b: Element) =>
      b.textContent?.includes('Claude Sonnet 4.6'),
    ) as HTMLElement;
    expect(card.textContent).toContain('retiring');
  });

  it('says a retired model already runs as its successor', async () => {
    const { page } = await mount('opus-4-1');
    const notice = page.retiringModelNotice()!;
    expect(notice.lead).toBe('Claude Opus 4.1 is retired.');
    expect(notice.detail).toBe('Claude Sonnet 5 now answers in its place.');
  });

  it('says an agent on a retired model with no successor cannot run', async () => {
    const { page } = await mount('claude-3');
    expect(page.retiringModelNotice()!.action).toContain('can’t run until you choose another model');
  });

  it('lets the author remove a retiring model but never add one back', async () => {
    const { page } = await mount('sonnet-4-6');
    page.selectModel('sonnet-5');
    expect(page.selectedModelId()).toBe('sonnet-5');
    // Once moved off it, it is gone from the grid and cannot be re-chosen.
    expect(refs(page)).not.toContain('sonnet-4-6');
    page.selectModel('sonnet-4-6');
    expect(page.selectedModelId()).toBe('sonnet-5');
  });

  it('treats a model with no status as active', async () => {
    const { page } = await mount(null);
    page.selectModel('haiku-4-5');
    expect(page.selectedModelId()).toBe('haiku-4-5');
  });
});
