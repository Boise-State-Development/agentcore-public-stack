import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, provideRouter } from '@angular/router';
import { AgentTemplateFormPage } from './agent-template-form.page';
import { AdminAgentTemplatesService } from './services/admin-agent-templates.service';
import { AgentTemplateAdmin } from './models/agent-template-admin.model';
import { AgentService } from '../../agents/services/agent.service';
import { BindableItem } from '../../agents/models/agent.model';

/** The RBAC-filtered model palette the dropdown reuses (via AgentService.loadBindable). */
const MODELS: BindableItem[] = [
  { kind: 'model', ref: 'us.anthropic.claude-sonnet-5', label: 'Claude Sonnet 5', description: '', meta: {} },
  { kind: 'model', ref: 'us.anthropic.claude-haiku-4', label: 'Claude Haiku 4', description: '', meta: {} },
];

/** The RBAC-filtered tool palette the binding dropdown reuses. */
const TOOLS: BindableItem[] = [
  { kind: 'tool', ref: 'gateway_search', label: 'Gateway Search', description: '', meta: {} },
  { kind: 'tool', ref: 'code_interpreter', label: 'Code Interpreter', description: '', meta: {} },
];

function fullTemplate(): AgentTemplateAdmin {
  return {
    template_id: 'course-helper',
    name: 'Course Helper',
    description: 'A study companion',
    emoji: '🎓',
    instructions: 'Help the student learn.',
    tags: ['edu', 'study'],
    starters: ['Explain this topic', 'Quiz me'],
    modelConfig: { modelId: 'us.anthropic.claude-sonnet-5', params: { temperature: 0.4 } },
    bindings: [{ kind: 'tool', ref: 'gateway_search', config: { scoped: true } }],
    pitch: 'Study companion',
    status: 'enabled',
    sort_order: 3,
    created_at: '2026-09-16T00:00:00Z',
    updated_at: '2026-09-16T00:00:00Z',
  };
}

function configure(
  id: string | null,
  service: Partial<AdminAgentTemplatesService>,
  models: BindableItem[] = MODELS,
  tools: BindableItem[] = TOOLS,
) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideRouter([]),
      { provide: AdminAgentTemplatesService, useValue: service },
      {
        provide: AgentService,
        useValue: {
          loadBindable: vi
            .fn()
            .mockImplementation((kind: string) =>
              Promise.resolve(kind === 'tool' ? tools : models),
            ),
        },
      },
      { provide: ActivatedRoute, useValue: { snapshot: { paramMap: { get: () => id } } } },
    ],
  });
  return TestBed.createComponent(AgentTemplateFormPage).componentInstance as any;
}

describe('AgentTemplateFormPage', () => {
  beforeEach(() => vi.clearAllMocks());

  describe('create mode', () => {
    const service = {
      createTemplate: vi.fn().mockResolvedValue({ template_id: 'document-q-a' }),
      updateTemplate: vi.fn(),
      getTemplate: vi.fn(),
    };

    it('buildPayload maps fields, parses tags, and nulls a blank model id', () => {
      const c = configure(null, service);
      c.form.patchValue({
        name: 'Document Q&A',
        emoji: '📚',
        pitch: 'Answers from docs',
        tagsCsv: 'qa,  docs ,',
        modelId: '   ',
      });
      c.addStarter();
      c.starters.at(0).setValue('What can you do?');
      c.addStarter(); // left blank — should be filtered out
      c.addBinding();
      c.bindings.at(0).patchValue({ kind: 'tool', ref: 'search' });

      const payload = c.buildPayload();
      expect(payload.name).toBe('Document Q&A');
      expect(payload.tags).toEqual(['qa', 'docs']);
      expect(payload.starters).toEqual(['What can you do?']);
      expect(payload.modelConfig).toEqual({ modelId: null, params: {} });
      expect(payload.bindings).toEqual([{ kind: 'tool', ref: 'search', config: {} }]);
      expect(payload.template_id).toBeUndefined();
    });

    it('includes a slug as template_id when provided on create', () => {
      const c = configure(null, service);
      c.form.patchValue({ name: 'My Template', slug: 'my-template' });
      expect(c.buildPayload().template_id).toBe('my-template');
    });

    it('onSubmit calls createTemplate (not update)', async () => {
      const c = configure(null, service);
      c.form.patchValue({ name: 'Document Q&A' });
      await c.onSubmit();
      expect(service.createTemplate).toHaveBeenCalledTimes(1);
      expect(service.updateTemplate).not.toHaveBeenCalled();
    });

    it('defaults to Platform default (modelId null) for a new template', async () => {
      const c = configure(null, service);
      await c.ngOnInit(); // create mode: loads the model palette, no template
      // The control's default is '' (Platform default), which serializes to null.
      expect(c.form.controls.modelId.value).toBe('');
      expect(c.models().length).toBe(MODELS.length);
      c.form.patchValue({ name: 'Blank Model' });
      expect(c.buildPayload().modelConfig).toEqual({ modelId: null, params: {} });
      expect(c.staleModelId()).toBeNull();
    });

    it('sets modelConfig.modelId to the chosen model ref when one is selected', async () => {
      const c = configure(null, service);
      await c.ngOnInit();
      c.form.patchValue({ name: 'Pinned Model', modelId: 'us.anthropic.claude-haiku-4' });
      expect(c.buildPayload().modelConfig).toEqual({
        modelId: 'us.anthropic.claude-haiku-4',
        params: {},
      });
      expect(c.staleModelId()).toBeNull();
    });

    it('loads the tool palette and adds a tool binding chosen from it', async () => {
      const c = configure(null, service);
      await c.ngOnInit();
      expect(c.tools().length).toBe(TOOLS.length);

      c.form.patchValue({ name: 'With Tool' });
      c.addBinding(); // defaults to kind 'tool', empty ref
      c.bindings.at(0).patchValue({ kind: 'tool', ref: 'code_interpreter' });

      expect(c.isKnownToolRef('code_interpreter')).toBe(true);
      expect(c.isKnownToolRef('not_a_tool')).toBe(false);
      expect(c.buildPayload().bindings).toEqual([
        { kind: 'tool', ref: 'code_interpreter', config: {} },
      ]);
    });
  });

  describe('edit mode', () => {
    it('loads the template into the form and PATCHes on submit', async () => {
      const service = {
        getTemplate: vi.fn().mockResolvedValue(fullTemplate()),
        updateTemplate: vi.fn().mockResolvedValue(fullTemplate()),
        createTemplate: vi.fn(),
      };
      const c = configure('course-helper', service);
      await c.ngOnInit();

      expect(c.isEdit()).toBe(true);
      expect(c.form.controls.name.value).toBe('Course Helper');
      expect(c.form.controls.modelId.value).toBe('us.anthropic.claude-sonnet-5');
      // The saved model is in the catalog, so it preselects cleanly (not flagged stale).
      expect(c.staleModelId()).toBeNull();
      expect(c.form.controls.tagsCsv.value).toBe('edu, study');
      expect(c.starters.length).toBe(2);
      expect(c.bindings.length).toBe(1);

      await c.onSubmit();
      expect(service.updateTemplate).toHaveBeenCalledTimes(1);
      const [id, payload] = service.updateTemplate.mock.calls[0];
      expect(id).toBe('course-helper');
      // preserved model params + binding config round-trip through the form
      expect(payload.modelConfig.params).toEqual({ temperature: 0.4 });
      expect(payload.bindings[0].config).toEqual({ scoped: true });
      expect(service.createTemplate).not.toHaveBeenCalled();
    });

    it('flags a saved model that is no longer in the catalog as unavailable, keeping it selected', async () => {
      const stale = fullTemplate();
      stale.modelConfig = { modelId: 'us.anthropic.retired-model-v1', params: {} };
      const service = {
        getTemplate: vi.fn().mockResolvedValue(stale),
        updateTemplate: vi.fn().mockResolvedValue(stale),
        createTemplate: vi.fn(),
      };
      // Catalog does NOT contain the saved model.
      const c = configure('course-helper', service, MODELS);
      await c.ngOnInit();

      // Kept selected (not dropped) and surfaced as the "unavailable" option.
      expect(c.form.controls.modelId.value).toBe('us.anthropic.retired-model-v1');
      expect(c.staleModelId()).toBe('us.anthropic.retired-model-v1');
      // Saving preserves the admin's value rather than silently nulling it.
      expect(c.buildPayload().modelConfig.modelId).toBe('us.anthropic.retired-model-v1');
    });

    it('keeps a saved tool ref not in the catalog as unavailable and preserves it on save', async () => {
      const stale = fullTemplate();
      stale.bindings = [{ kind: 'tool', ref: 'retired_tool_v1', config: {} }];
      const service = {
        getTemplate: vi.fn().mockResolvedValue(stale),
        updateTemplate: vi.fn().mockResolvedValue(stale),
        createTemplate: vi.fn(),
      };
      const c = configure('course-helper', service);
      await c.ngOnInit();

      // The saved tool binding stays, flagged unavailable (not in the catalog).
      expect(c.bindings.at(0).controls.ref.value).toBe('retired_tool_v1');
      expect(c.isKnownToolRef('retired_tool_v1')).toBe(false);
      expect(c.isKnownToolRef('gateway_search')).toBe(true);
      // Preserved on save rather than dropped.
      expect(c.buildPayload().bindings).toEqual([
        { kind: 'tool', ref: 'retired_tool_v1', config: {} },
      ]);
    });
  });
});
