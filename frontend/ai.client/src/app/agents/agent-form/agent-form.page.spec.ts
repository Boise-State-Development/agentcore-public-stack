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
  modelLabel = input('');
  toolCount = input(0);
  skillCount = input(0);
  memoryCount = input(0);
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
    fixture.detectChanges();
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
});
