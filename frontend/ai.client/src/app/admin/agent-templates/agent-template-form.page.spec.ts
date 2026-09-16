import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, provideRouter } from '@angular/router';
import { AgentTemplateFormPage } from './agent-template-form.page';
import { AdminAgentTemplatesService } from './services/admin-agent-templates.service';
import { AgentTemplateAdmin } from './models/agent-template-admin.model';

function fullTemplate(): AgentTemplateAdmin {
  return {
    template_id: 'course-helper',
    name: 'Course Helper',
    description: 'A study companion',
    emoji: '🎓',
    instructions: 'Help the student learn.',
    visibility: 'PRIVATE',
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

function configure(id: string | null, service: Partial<AdminAgentTemplatesService>) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideRouter([]),
      { provide: AdminAgentTemplatesService, useValue: service },
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
  });
});
