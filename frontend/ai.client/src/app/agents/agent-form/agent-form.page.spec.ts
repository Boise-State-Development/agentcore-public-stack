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
import { ToolService } from '../../services/tool/tool.service';
import { AGENT_TEMPLATE_DRAFT_KEY, TemplateDraft } from './agent-templates';

/**
 * A representative "finished" template draft, inlined here now that the hardcoded catalog
 * has moved to the backend. This suite tests the FORM's prefill/reconcile behavior, not
 * catalog content, so a local fixture is the right dependency — it exercises the same
 * population path (name/emoji/description/instructions/starters/model/tool binding) the
 * real templates flow through, without coupling the form test to any org's template data.
 */
const COURSE_HELPER_DRAFT: TemplateDraft = {
  templateId: 'course-helper',
  name: 'Course Helper',
  description: 'A study assistant for a single course, grounded in your materials.',
  emoji: '🎓',
  instructions:
    'You are a Course Helper.\n\n## Academic integrity\nDo not do graded work for the student; teach the concept instead.',
  tags: [],
  starters: ['What topics does this course cover?', 'When is the next assignment due?'],
  modelConfig: { modelId: 'us.anthropic.claude-sonnet-5', params: {} },
  bindings: [{ kind: 'tool', ref: 'gateway_search_boise_state', config: {} }],
};

/**
 * A stand-in for the root {@link ToolService}. The real one loads `/tools/` from its
 * constructor via HttpClient (not provided in this suite), and the create-mode form now
 * injects it to reconcile a template's tool refs. `tools()` returns the catalog under
 * test; `initialized()` short-circuits the form's lazy `loadTools()`.
 */
function makeToolService(catalog: { toolId: string; status: string }[] = []) {
  return {
    initialized: () => true,
    tools: () => catalog,
    loadTools: vi.fn().mockResolvedValue(undefined),
  };
}

/**
 * Stand-ins for the two heavy children — this suite only exercises the form shell.
 * Every bound input must be declared or the template binding throws NG0303.
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

/**
 * Drain the microtask queue so the load promises' `.finally` handlers (which clear
 * the loading flags) have run, then render. `whenStable()` alone can resolve ahead
 * of them and leave the fixture showing its skeleton.
 */
async function settle(fixture: ComponentFixture<AgentFormPage>): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  fixture.detectChanges();
}

describe('AgentFormPage — invalid submit feedback', () => {
  let component: AgentFormPage;
  let fixture: ComponentFixture<AgentFormPage>;

  const mockAgentService = {
    loadBindable: vi.fn().mockResolvedValue([]),
    createAgent: vi.fn().mockResolvedValue({ agentId: 'agt-1' }),
    updateAgent: vi.fn().mockResolvedValue({ agentId: 'agt-1' }),
    getAgent: vi.fn().mockResolvedValue(undefined),
  };
  const mockToast = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };
  const mockSidenav = { hide: vi.fn(), show: vi.fn() };
  const mockTheme = { isDark: () => false };

  beforeEach(async () => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();

    // jsdom has no layout engine and so implements no scrollIntoView; without this
    // the reveal-on-invalid path throws instead of exercising the behaviour.
    Element.prototype.scrollIntoView = vi.fn();

    TestBed.configureTestingModule({
      imports: [ReactiveFormsModule],
      providers: [
        // Register the route a successful save navigates to. An empty
        // `provideRouter([])` makes that navigation reject with NG04002 as an
        // unhandled rejection after the test body, failing the whole run.
        provideRouter([{ path: 'agents', children: [] }]),
        { provide: ToolService, useValue: makeToolService() },
        { provide: AgentService, useValue: mockAgentService },
        { provide: ToastService, useValue: mockToast },
        { provide: SidenavService, useValue: mockSidenav },
        { provide: ThemeService, useValue: mockTheme },
        // Create mode: no :id on the route.
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: { get: () => null } } } },
      ],
    });

    TestBed.overrideComponent(AgentFormPage, {
      remove: { imports: [AgentPreviewComponent, KnowledgeBaseSectionComponent] },
      add: { imports: [StubPreviewComponent, StubKnowledgeBaseComponent] },
    });

    fixture = TestBed.createComponent(AgentFormPage);
    component = fixture.componentInstance;
    fixture.detectChanges();
    await fixture.whenStable();
    await settle(fixture);
  });

  /** Fill every required control except the one named, so it is the first invalid. */
  function fillAllExcept(skip: 'name' | 'description' | 'instructions'): void {
    const values: Record<string, string> = {
      name: 'Research Assistant',
      description: 'A short summary of the agent',
      instructions: 'You are a helpful assistant that answers questions.',
    };
    delete values[skip];
    component.form.patchValue(values);
    fixture.detectChanges();
  }

  it('shows an error toast and issues no request when the form is invalid', async () => {
    fillAllExcept('description');

    await component.onSubmit();

    expect(mockAgentService.createAgent).not.toHaveBeenCalled();
    expect(mockToast.error).toHaveBeenCalledWith('Fix the highlighted fields before saving.');
    expect(mockToast.success).not.toHaveBeenCalled();
  });

  it('scrolls the first invalid control into view and focuses it', async () => {
    fillAllExcept('description');

    const scrollSpy = vi.fn();
    const description: HTMLInputElement =
      fixture.nativeElement.querySelector('input#description');
    description.scrollIntoView = scrollSpy;

    await component.onSubmit();

    expect(scrollSpy).toHaveBeenCalledWith({ behavior: 'smooth', block: 'center' });
    expect(document.activeElement).toBe(description);
  });

  it('marks the untouched invalid controls as touched so inline errors render', async () => {
    fillAllExcept('description');
    expect(component.form.get('description')?.touched).toBe(false);

    await component.onSubmit();

    expect(component.form.get('description')?.touched).toBe(true);
  });

  it('saves normally when every required field is filled', async () => {
    component.form.patchValue({
      name: 'Research Assistant',
      description: 'A short summary of the agent',
      instructions: 'You are a helpful assistant that answers questions.',
    });
    component.selectedModelId.set('anthropic.claude-opus-4-8');
    fixture.detectChanges();

    await component.onSubmit();

    expect(mockAgentService.createAgent).toHaveBeenCalled();
    expect(mockToast.error).not.toHaveBeenCalled();
  });

  it('is done loading once the palettes resolve', () => {
    expect(component.loading()).toBe(false);
    expect(fixture.nativeElement.querySelector('form')).toBeTruthy();
  });
});

describe('AgentFormPage — loading state', () => {
  let fixture: ComponentFixture<AgentFormPage>;
  let component: AgentFormPage;
  /** Resolve to let the in-flight palette + agent fetches settle. */
  let releasePalettes: () => void;
  let releaseAgent: () => void;

  const mockToast = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };

  beforeEach(async () => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();

    const palettes = new Promise<[]>((resolve) => {
      releasePalettes = () => resolve([]);
    });
    const agent = new Promise<Record<string, unknown>>((resolve) => {
      releaseAgent = () => resolve({ name: 'Loaded', userPermission: 'owner' });
    });

    TestBed.configureTestingModule({
      imports: [ReactiveFormsModule],
      providers: [
        provideRouter([{ path: 'agents', children: [] }]),
        { provide: ToolService, useValue: makeToolService() },
        {
          provide: AgentService,
          useValue: {
            loadBindable: vi.fn().mockReturnValue(palettes),
            getAgent: vi.fn().mockReturnValue(agent),
          },
        },
        { provide: ToastService, useValue: mockToast },
        { provide: SidenavService, useValue: { hide: vi.fn(), show: vi.fn() } },
        { provide: ThemeService, useValue: { isDark: () => false } },
        // Edit mode: an :id on the route, so the record fetch runs too.
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: { get: () => 'agt-1' } } } },
      ],
    });

    TestBed.overrideComponent(AgentFormPage, {
      remove: { imports: [AgentPreviewComponent, KnowledgeBaseSectionComponent] },
      add: { imports: [StubPreviewComponent, StubKnowledgeBaseComponent] },
    });

    fixture = TestBed.createComponent(AgentFormPage);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('shows a skeleton instead of the form while the page loads', () => {
    expect(component.loading()).toBe(true);
    expect(fixture.nativeElement.querySelector('form')).toBeNull();
    expect(fixture.nativeElement.querySelector('.animate-pulse')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('[role="status"]')?.textContent).toContain(
      'Loading agent',
    );
  });

  it('disables Save while loading', () => {
    const save: HTMLButtonElement | null = fixture.nativeElement.querySelector(
      'header button[type="button"]:last-of-type',
    );
    expect(save?.disabled).toBe(true);
  });

  it('stays on the skeleton until BOTH the palettes and the record settle', async () => {
    releasePalettes();
    await settle(fixture);
    expect(component.loading()).toBe(true);
    expect(fixture.nativeElement.querySelector('form')).toBeNull();

    releaseAgent();
    await settle(fixture);
    expect(component.loading()).toBe(false);
    expect(fixture.nativeElement.querySelector('form')).toBeTruthy();
  });
});

/**
 * A saved enum param (reasoning effort) must still be the one selected when the
 * author reopens the agent. The select's options are rendered by `@for` from the
 * model's `supportedParams`, so binding the saved value on the <select> loses it:
 * `select.value` is written before the options mount, the browser drops it, and
 * Angular never re-applies a binding whose value did not change — a saved `high`
 * silently reads back as the model's default. Same defect as #161.
 */
describe('AgentFormPage — saved enum param on reopen', () => {
  let fixture: ComponentFixture<AgentFormPage>;
  let component: AgentFormPage;

  const MODEL_WITH_EFFORT = {
    kind: 'model',
    ref: 'openai.gpt-5.4',
    label: 'GPT-5.4',
    description: '',
    meta: {
      provider: 'mantle',
      supportedParams: {
        params: {
          reasoning_effort: {
            supported: true,
            allowed: ['low', 'medium', 'high'],
            default: 'medium',
            locked: false,
          },
        },
      },
    },
  };

  const mockAgentService = {
    loadBindable: vi
      .fn()
      .mockImplementation((kind: string) =>
        Promise.resolve(kind === 'model' ? [MODEL_WITH_EFFORT] : []),
      ),
    getAgent: vi.fn().mockResolvedValue({
      agentId: 'agt-1',
      name: 'Research Assistant',
      description: 'A short summary of the agent',
      instructions: 'You are a helpful assistant that answers questions.',
      userPermission: 'owner',
      modelConfig: {
        modelId: 'openai.gpt-5.4',
        provider: 'mantle',
        params: { reasoning_effort: 'high' },
      },
      bindings: [],
    }),
    updateAgent: vi.fn().mockResolvedValue({ agentId: 'agt-1' }),
  };

  beforeEach(async () => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    Element.prototype.scrollIntoView = vi.fn();

    TestBed.configureTestingModule({
      imports: [ReactiveFormsModule],
      providers: [
        provideRouter([{ path: 'agents', children: [] }]),
        { provide: ToolService, useValue: makeToolService() },
        { provide: AgentService, useValue: mockAgentService },
        {
          provide: ToastService,
          useValue: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
        },
        { provide: SidenavService, useValue: { hide: vi.fn(), show: vi.fn() } },
        { provide: ThemeService, useValue: { isDark: () => false } },
        // Edit mode: the route carries the agent id.
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: { get: () => 'agt-1' } } } },
      ],
    });

    TestBed.overrideComponent(AgentFormPage, {
      remove: { imports: [AgentPreviewComponent, KnowledgeBaseSectionComponent] },
      add: { imports: [StubPreviewComponent, StubKnowledgeBaseComponent] },
    });

    fixture = TestBed.createComponent(AgentFormPage);
    component = fixture.componentInstance;
    fixture.detectChanges();
    await fixture.whenStable();
    await settle(fixture);
  });

  function effortSelect(): HTMLSelectElement {
    return fixture.nativeElement.querySelector('select#param-reasoning_effort');
  }

  it('hydrates the saved param into component state', () => {
    expect(component.paramValue('reasoning_effort')).toBe('high');
  });

  it('renders the saved value as the selected option, not the model default', () => {
    const select = effortSelect();
    expect(select).toBeTruthy();
    expect([...select.options].map((o) => o.value)).toEqual(['', 'low', 'medium', 'high']);
    expect(select.value).toBe('high');
  });

  it('still round-trips a change back through the form state', () => {
    const select = effortSelect();
    select.value = 'low';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();

    expect(component.paramValue('reasoning_effort')).toBe('low');
    expect(effortSelect().value).toBe('low');
  });

  it('still sends the saved value on a save that never touched the control', async () => {
    // The regression was display-only: `modelParams` kept `high` even while the
    // select showed the default, so reopening and saving never rewrote the record.
    await component.onSubmit();

    expect(mockAgentService.updateAgent).toHaveBeenCalledWith(
      'agt-1',
      expect.objectContaining({
        modelConfig: expect.objectContaining({ params: { reasoning_effort: 'high' } }),
      }),
    );
  });

  it('falls back to the Default option when the param is cleared', () => {
    const select = effortSelect();
    select.value = '';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();

    expect(component.paramValue('reasoning_effort')).toBe('');
    expect(effortSelect().value).toBe('');
  });
});

/**
 * Agent Template Prefill — Phase 2. In create mode the form reads a template draft from
 * `localStorage[AGENT_TEMPLATE_DRAFT_KEY]`, reconciles its tool refs against the live tool
 * catalog, populates every field through the same `applyAgentToForm` path edit mode uses,
 * clears the key (one-shot), leaves the form dirty, and surfaces flagged/dropped notices.
 */
describe('AgentFormPage — template prefill (create mode)', () => {
  let fixture: ComponentFixture<AgentFormPage>;
  let component: AgentFormPage;

  const mockToast = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };

  /** Configure the module for create mode with the given tool catalog, then build+settle. */
  async function bootstrap(
    catalog: { toolId: string; status: string }[],
  ): Promise<void> {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    Element.prototype.scrollIntoView = vi.fn();

    TestBed.configureTestingModule({
      imports: [ReactiveFormsModule],
      providers: [
        provideRouter([{ path: 'agents', children: [] }]),
        { provide: ToolService, useValue: makeToolService(catalog) },
        {
          provide: AgentService,
          useValue: {
            loadBindable: vi.fn().mockResolvedValue([]),
            createAgent: vi.fn().mockResolvedValue({ agentId: 'agt-1' }),
            updateAgent: vi.fn().mockResolvedValue({ agentId: 'agt-1' }),
            getAgent: vi.fn().mockResolvedValue(undefined),
          },
        },
        { provide: ToastService, useValue: mockToast },
        { provide: SidenavService, useValue: { hide: vi.fn(), show: vi.fn() } },
        { provide: ThemeService, useValue: { isDark: () => false } },
        // Create mode: no :id on the route.
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: { get: () => null } } } },
      ],
    });

    TestBed.overrideComponent(AgentFormPage, {
      remove: { imports: [AgentPreviewComponent, KnowledgeBaseSectionComponent] },
      add: { imports: [StubPreviewComponent, StubKnowledgeBaseComponent] },
    });

    fixture = TestBed.createComponent(AgentFormPage);
    component = fixture.componentInstance;
    fixture.detectChanges();
    await fixture.whenStable();
    await settle(fixture);
    // The prefill runs after the palettes promise resolves; give that chain a beat.
    await settle(fixture);
  }

  beforeEach(() => {
    localStorage.clear();
  });

  it('populates every field from a valid draft and clears the key (one-shot)', async () => {
    const draft = COURSE_HELPER_DRAFT;
    localStorage.setItem(AGENT_TEMPLATE_DRAFT_KEY, JSON.stringify(draft));

    await bootstrap([{ toolId: 'gateway_search_boise_state', status: 'active' }]);

    expect(component.form.get('name')?.value).toBe('Course Helper');
    expect(component.form.get('description')?.value).toBe(draft.description);
    expect(component.form.get('instructions')?.value).toContain('Academic integrity');
    expect(component.form.get('visibility')?.value).toBe('PRIVATE');
    expect(component.form.get('emoji')?.value).toBe('🎓');
    expect(component.starters.length).toBe(draft.starters.length);
    // The template's chosen model is selected (P1's applyAgentToForm sets selectedModelId).
    expect(draft.modelConfig.modelId).toBeTruthy();
    expect(component.selectedModelId()).toBe(draft.modelConfig.modelId);
    // The single active tool binding is applied.
    expect(component.selectedToolRefs().has('gateway_search_boise_state')).toBe(true);
    // One-shot: the key is consumed.
    expect(localStorage.getItem(AGENT_TEMPLATE_DRAFT_KEY)).toBeNull();
    // Prefilled-but-unsaved reads as a dirty draft.
    expect(component.isDirty()).toBe(true);
    // A clean, fully-active draft raises no notice.
    expect(component.hasTemplateNotice()).toBe(false);
  });

  it('applies a deprecated ref but flags it, and drops an unknown ref with a notice', async () => {
    const draft: TemplateDraft = {
      templateId: 'test-mixed',
      name: 'Mixed Tools',
      description: 'A template exercising reconcile outcomes.',
      emoji: '🧪',
      instructions: 'You are a test agent used to verify tool-ref reconciliation behavior.',
      tags: [],
      starters: [],
      modelConfig: { modelId: null, params: {} },
      bindings: [
        { kind: 'tool', ref: 'gateway_search_boise_state', config: {} },
        { kind: 'tool', ref: 'legacy_tool', config: {} },
        { kind: 'tool', ref: 'ghost_tool', config: {} },
      ],
    };
    localStorage.setItem(AGENT_TEMPLATE_DRAFT_KEY, JSON.stringify(draft));

    await bootstrap([
      { toolId: 'gateway_search_boise_state', status: 'active' },
      { toolId: 'legacy_tool', status: 'deprecated' },
    ]);

    const refs = component.selectedToolRefs();
    // Active + deprecated are applied; unknown is not.
    expect(refs.has('gateway_search_boise_state')).toBe(true);
    expect(refs.has('legacy_tool')).toBe(true);
    expect(refs.has('ghost_tool')).toBe(false);
    // Both notices surface.
    expect(component.hasTemplateNotice()).toBe(true);
    expect(component.templateFlaggedNotice()).toContain('legacy_tool');
    expect(component.templateFlaggedNotice()).toContain('deprecated');
    expect(component.templateDroppedNotice()).toContain('ghost_tool');
    // Dismiss clears both.
    component.dismissTemplateNotice();
    expect(component.hasTemplateNotice()).toBe(false);
  });

  it('behaves as a blank create when no draft is present', async () => {
    await bootstrap([{ toolId: 'gateway_search_boise_state', status: 'active' }]);

    expect(component.form.get('name')?.value).toBe('');
    expect(component.selectedToolRefs().size).toBe(0);
    expect(component.hasTemplateNotice()).toBe(false);
    // Nothing prefilled ⇒ the form is untouched/pristine.
    expect(component.isDirty()).toBe(false);
  });

  it('clears the key and starts blank when the stored draft is malformed JSON', async () => {
    localStorage.setItem(AGENT_TEMPLATE_DRAFT_KEY, '{ not valid json');

    await bootstrap([]);

    expect(component.form.get('name')?.value).toBe('');
    expect(localStorage.getItem(AGENT_TEMPLATE_DRAFT_KEY)).toBeNull();
    expect(mockToast.error).toHaveBeenCalled();
  });
});
