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
import { toggleChild, toggleItem } from '../../components/tool-selector/tool-selection';

/**
 * Binding a subset of an MCP server's tools, from the Agent Designer's side.
 *
 * The selection rules (a fully-selected server is stored as the **bare** ref, never
 * as N scoped ones) live in `components/tool-selector/tool-selection.ts` and are
 * pinned there. What this pins is the page's half: `selectedToolRefs` hydrates the
 * `binding.ref` values verbatim, the rows it hands the selector carry each server's
 * tools, and what the selector reports is exactly what the form submits.
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

const CANVAS = {
  kind: 'tool',
  ref: 'canvas_faculty',
  label: 'Canvas Faculty',
  description: 'Canvas LMS',
  meta: {
    protocol: 'mcp_external',
    serverTools: [
      { name: 'list_courses', description: 'List courses.\n\nArgs:\n  term: the term' },
      { name: 'list_rubrics', description: 'List rubrics.' },
      { name: 'grade_submission', description: 'Grade a submission.' },
    ],
  },
};

/** A local tool: nothing to narrow, so it must never offer the per-tool control. */
const CALCULATOR = {
  kind: 'tool',
  ref: 'calculator',
  label: 'Calculator',
  description: 'Arithmetic',
  meta: { protocol: 'direct', serverTools: [] },
};

async function settle(fixture: ComponentFixture<AgentFormPage>): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  fixture.detectChanges();
}

async function mount(
  bindings: { kind: string; ref: string }[],
  agentId: string | null = 'agt-1',
): Promise<{
  fixture: ComponentFixture<AgentFormPage>;
  component: AgentFormPage;
}> {
  TestBed.resetTestingModule();
  vi.clearAllMocks();
  Element.prototype.scrollIntoView = vi.fn();

  const agentService = {
    loadBindable: vi
      .fn()
      .mockImplementation((kind: string) =>
        Promise.resolve(kind === 'tool' ? [CANVAS, CALCULATOR] : []),
      ),
    getAgent: vi.fn().mockResolvedValue({
      agentId: 'agt-1',
      name: 'Rubric Builder',
      description: 'Builds rubrics',
      instructions: 'You build rubrics.',
      visibility: 'PRIVATE',
      userPermission: 'owner',
      bindings,
    }),
    createAgent: vi.fn().mockResolvedValue({ agentId: 'agt-1' }),
    updateAgent: vi.fn().mockResolvedValue({ agentId: 'agt-1' }),
  };

  TestBed.configureTestingModule({
    imports: [ReactiveFormsModule],
    providers: [
      provideRouter([{ path: 'agents', children: [] }]),
      // The create-mode form injects ToolService; stub it so the root service's
      // constructor doesn't attempt a (blocked) real GET /tools/.
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
      { provide: ActivatedRoute, useValue: { snapshot: { paramMap: { get: () => agentId } } } },
    ],
  });

  TestBed.overrideComponent(AgentFormPage, {
    remove: { imports: [AgentPreviewComponent, KnowledgeBaseSectionComponent] },
    add: { imports: [StubPreviewComponent, StubKnowledgeBaseComponent] },
  });

  const fixture = TestBed.createComponent(AgentFormPage);
  fixture.detectChanges();
  await fixture.whenStable();
  await settle(fixture);
  return { fixture, component: fixture.componentInstance };
}

/** The refs the form would submit, order-insensitive. */
function refs(component: AgentFormPage): string[] {
  return [...component.selectedToolRefs()].sort();
}

const CANVAS_TOOLS = CANVAS.meta.serverTools.map((t) => t.name);

/** What `<app-tool-selector>` emits for a row click, handed to the page's handler. */
function clickRow(component: AgentFormPage, ref: string): void {
  component.onToolSelectionChange(toggleItem(component.selectedToolRefs(), ref));
}

/** What the selector emits for one of a server's tools. */
function clickServerTool(component: AgentFormPage, name: string): void {
  component.onToolSelectionChange(
    toggleChild(component.selectedToolRefs(), 'canvas_faculty', CANVAS_TOOLS, name),
  );
}

function canvasItem(component: AgentFormPage) {
  return component.toolItems().find((i) => i.id === 'canvas_faculty')!;
}

describe('AgentFormPage — scoped tool bindings', () => {
  let fixture: ComponentFixture<AgentFormPage>;
  let component: AgentFormPage;

  describe('a whole-server binding', () => {
    beforeEach(async () => {
      ({ fixture, component } = await mount([{ kind: 'tool', ref: 'canvas_faculty' }]));
    });

    it('reads as selected', () => {
      expect(component.isToolSelected('canvas_faculty')).toBe(true);
    });

    it('stays the bare ref when nothing is touched', () => {
      expect(refs(component)).toEqual(['canvas_faculty']);
      expect(component.isDirty()).toBe(false);
    });

    it('writes the narrowed refs the selector reports, and marks the form dirty', () => {
      clickServerTool(component, 'grade_submission');
      expect(refs(component)).toEqual([
        'canvas_faculty::list_courses',
        'canvas_faculty::list_rubrics',
      ]);
      expect(component.isDirty()).toBe(true);
    });

    it('collapses back to the bare ref when the last tool is turned on again', () => {
      clickServerTool(component, 'grade_submission');
      clickServerTool(component, 'grade_submission');
      expect(refs(component)).toEqual(['canvas_faculty']);
    });
  });

  describe('a scoped binding', () => {
    beforeEach(async () => {
      ({ fixture, component } = await mount([
        { kind: 'tool', ref: 'canvas_faculty::list_courses' },
        { kind: 'tool', ref: 'canvas_faculty::list_rubrics' },
      ]));
    });

    it('hydrates its refs verbatim', () => {
      expect(refs(component)).toEqual([
        'canvas_faculty::list_courses',
        'canvas_faculty::list_rubrics',
      ]);
    });

    it('shows the server as selected', () => {
      expect(component.isToolSelected('canvas_faculty')).toBe(true);
      expect(component.selectedToolCount()).toBe(1);
    });

    it('drops every scoped ref when the server row is deselected', () => {
      clickRow(component, 'canvas_faculty');
      expect(refs(component)).toEqual([]);
    });

    it('re-selecting the row binds the whole server again', () => {
      clickRow(component, 'canvas_faculty');
      clickRow(component, 'canvas_faculty');
      expect(refs(component)).toEqual(['canvas_faculty']);
    });

    it('renders the per-tool rows once expanded, with the narrowing on the row', () => {
      // A saved agent with tools opens collapsed, so the panel has to be opened
      // before its rows exist at all — see `toolsOpen`.
      component.toolsOpen.set(true);
      fixture.detectChanges();
      const root: HTMLElement = fixture.nativeElement;
      // `2 of 3` on the row, so the narrowing is legible without expanding.
      expect(root.textContent).toContain('2 of 3 tools');

      const expand = root.querySelector(
        'button[aria-label="Choose individual Canvas Faculty tools"]',
      ) as HTMLButtonElement;
      expand.click();
      fixture.detectChanges();
      const panel = root.querySelector(`#${expand.getAttribute('aria-controls')}`)!;
      const boxes = [...panel.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')];
      expect(boxes.map((b) => b.checked)).toEqual([true, true, false]);
      expect(panel.textContent).toContain('grade_submission');

      // Ticking the last one through the real control collapses to the bare ref.
      boxes[2].click();
      fixture.detectChanges();
      expect(refs(component)).toEqual(['canvas_faculty']);
    });
  });

  describe('the selector rows', () => {
    beforeEach(async () => {
      ({ fixture, component } = await mount([]));
    });

    it('splits each server tool docstring into summary and Args: detail, like the chat picker', () => {
      const [listCourses] = canvasItem(component).children!;
      expect(listCourses.summary).toBe('List courses.');
      expect(listCourses.detail).toContain('Args:');
    });

    it('gives a tool with no discovered list no per-tool children', () => {
      const calculator = component.toolItems().find((i) => i.id === 'calculator')!;
      expect(calculator.children).toEqual([]);
    });

    it('groups by catalog category, sorted, with `Other` for an unset one', () => {
      // Neither fixture has a `category`, which is the point of the fallback: an
      // uncategorised tool still has a home.
      expect(component.toolItems().map((i) => [i.group, i.name])).toEqual([
        ['Other', 'Calculator'],
        ['Other', 'Canvas Faculty'],
      ]);
    });

    it('renders an empty state rather than a blank list when nothing matches', () => {
      const root: HTMLElement = fixture.nativeElement;
      const input = root.querySelector('#agent-tools-panel input[type="search"]') as HTMLInputElement;
      input.value = 'zzz-no-such-tool';
      input.dispatchEvent(new Event('input'));
      fixture.detectChanges();
      expect(root.textContent).toContain('No tools match');
    });

    it('selects a plain tool through the rendered checkbox', () => {
      const root: HTMLElement = fixture.nativeElement;
      const name = [...root.querySelectorAll('#agent-tools-panel [id$="-name"]')].find(
        (n) => n.textContent?.trim() === 'Calculator',
      )!;
      (root.querySelector(`input[aria-labelledby="${name.id}"]`) as HTMLInputElement).click();
      fixture.detectChanges();
      expect(refs(component)).toEqual(['calculator']);
      expect(component.isDirty()).toBe(true);
    });
  });

  /**
   * The Tools section is a disclosure, and the default it opens at is the whole
   * design: closed is only right when there is a summary to close *to*. A saved
   * agent gets its one-line answer; a new or empty one keeps the list in front of
   * the author, who otherwise has no way to learn what the platform can do.
   */
  describe('the Tools disclosure', () => {
    it('opens collapsed for a saved agent that already has tools', async () => {
      ({ component } = await mount([{ kind: 'tool', ref: 'canvas_faculty' }]));
      expect(component.toolsOpen()).toBe(false);
      expect(component.selectedToolCount()).toBe(1);
      expect(component.selectedToolSummary()).toBe('Canvas Faculty');
    });

    it('stays open for a saved agent with no tools bound', async () => {
      ({ component } = await mount([]));
      expect(component.toolsOpen()).toBe(true);
      expect(component.selectedToolSummary()).toBe('');
    });

    it('stays open in create mode', async () => {
      ({ component } = await mount([], null));
      expect(component.mode()).toBe('create');
      expect(component.toolsOpen()).toBe(true);
    });

    it('caps the summary at three names', async () => {
      ({ component } = await mount([
        { kind: 'tool', ref: 'canvas_faculty' },
        { kind: 'tool', ref: 'calculator' },
      ]));
      // Alphabetical, so the header does not reorder itself between visits.
      expect(component.selectedToolSummary()).toBe('Calculator, Canvas Faculty');
    });
  });
});
