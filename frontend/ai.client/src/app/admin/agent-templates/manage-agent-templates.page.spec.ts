import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { ManageAgentTemplatesPage } from './manage-agent-templates.page';
import { AdminAgentTemplatesService } from './services/admin-agent-templates.service';
import { ToastService } from '../../services/toast/toast.service';
import { ADMIN_SCOPE_IDS } from '../admin-scope.model';
import { AgentTemplateAdmin } from './models/agent-template-admin.model';

function tpl(overrides: Partial<AgentTemplateAdmin>): AgentTemplateAdmin {
  return {
    template_id: 'course-helper',
    name: 'Course Helper',
    description: '',
    emoji: '🎓',
    instructions: '',
    tags: [],
    starters: [],
    modelConfig: { modelId: null, params: {} },
    bindings: [],
    pitch: 'A study companion',
    status: 'enabled',
    sort_order: 0,
    created_at: '',
    updated_at: '',
    ...overrides,
  };
}

describe('ManageAgentTemplatesPage', () => {
  const rows: AgentTemplateAdmin[] = [
    tpl({ template_id: 'course-helper', name: 'Course Helper', status: 'enabled', sort_order: 0 }),
    tpl({ template_id: 'qa-helper', name: 'Q&A Assistant', status: 'disabled', sort_order: 1, emoji: '📚' }),
  ];

  const mockService = {
    ensureLoaded: vi.fn(),
    templatesResource: {
      value: () => ({ templates: rows, total: rows.length }),
      isLoading: () => false,
      error: () => null,
    },
    updateTemplate: vi.fn().mockResolvedValue({}),
    deleteTemplate: vi.fn().mockResolvedValue(undefined),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: AdminAgentTemplatesService, useValue: mockService },
        { provide: ToastService, useValue: { success: vi.fn(), error: vi.fn() } },
      ],
    });
  });

  it('requests the catalog on construction and renders every template', () => {
    const fixture = TestBed.createComponent(ManageAgentTemplatesPage);
    fixture.detectChanges();
    expect(mockService.ensureLoaded).toHaveBeenCalled();
    const text = fixture.nativeElement.textContent as string;
    expect(text).toContain('Course Helper');
    expect(text).toContain('Q&A Assistant');
  });

  it('sorts rows by sort_order then name', () => {
    const fixture = TestBed.createComponent(ManageAgentTemplatesPage);
    fixture.detectChanges();
    const ordered = (fixture.componentInstance as any).templates();
    expect(ordered.map((t: AgentTemplateAdmin) => t.template_id)).toEqual([
      'course-helper',
      'qa-helper',
    ]);
  });

  it('toggleEnabled PATCHes the flipped status', async () => {
    const fixture = TestBed.createComponent(ManageAgentTemplatesPage);
    fixture.detectChanges();
    await (fixture.componentInstance as any).toggleEnabled(rows[0]);
    expect(mockService.updateTemplate).toHaveBeenCalledWith('course-helper', { status: 'disabled' });
  });

  it('registers the admin.agent_templates scope for the route guard', () => {
    expect(ADMIN_SCOPE_IDS).toContain('admin.agent_templates');
  });
});
