import { TestBed, ComponentFixture } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { provideRouter, ActivatedRoute } from '@angular/router';
import { signal } from '@angular/core';
import { ScheduleFormPage } from './schedule-form.page';
import { ScheduleService } from '../services/schedule.service';
import { RunNowService } from '../services/run-now.service';
import { AgentService } from '../../agents/services/agent.service';
import { Tool, ToolService } from '../../services/tool/tool.service';
import { ToastService } from '../../services/toast/toast.service';

/**
 * The schedule's Tools picker, as rendered: the shared `<app-tool-selector>` fed
 * from the caller's RBAC-filtered `ToolService.tools()`, with the schedule's own
 * snapshot note and "Reset to all my tools" kept on the page.
 */

function tool(overrides: Partial<Tool>): Tool {
  return {
    toolId: 'x',
    displayName: 'X',
    description: '',
    category: 'search',
    icon: null,
    protocol: 'local',
    status: 'active',
    grantedBy: [],
    enabledByDefault: true,
    userEnabled: null,
    isEnabled: true,
    ...overrides,
  };
}

const TOOLS: Tool[] = [
  tool({ toolId: 'class_search', displayName: 'Class Search', description: 'Find classes', category: 'search' }),
  tool({ toolId: 'calculator', displayName: 'Calculator', description: 'Arithmetic', category: 'utility' }),
  tool({
    toolId: 'old_search',
    displayName: 'Old Search',
    description: 'Legacy',
    category: 'search',
    status: 'deprecated',
    retirementNote: 'Replaced by Class Search',
  }),
];

describe('ScheduleFormPage — tools picker', () => {
  let fixture: ComponentFixture<ScheduleFormPage>;
  let component: ScheduleFormPage;
  let el: HTMLElement;

  async function mount(scheduleId: string | null = null): Promise<void> {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([{ path: 'schedules', children: [] }]),
        {
          provide: ScheduleService,
          useValue: {
            getSchedule: vi.fn().mockResolvedValue({
              scheduleId: 'sched-1',
              assistantId: null,
              label: 'Briefing',
              promptText: 'Summarize',
              cadence: 'daily',
              hourLocal: 7,
              weekday: null,
              timezone: 'UTC',
              enabledTools: ['class_search'],
            }),
            createSchedule: vi.fn(),
            updateSchedule: vi.fn(),
          },
        },
        { provide: RunNowService, useValue: { run: vi.fn() } },
        { provide: AgentService, useValue: { agents$: signal([]), loadAgents: vi.fn().mockResolvedValue(undefined) } },
        {
          provide: ToolService,
          useValue: { tools: signal(TOOLS), initialized: signal(true), loadTools: vi.fn() },
        },
        { provide: ToastService, useValue: { success: vi.fn(), error: vi.fn(), info: vi.fn() } },
        {
          provide: ActivatedRoute,
          useValue: { snapshot: { paramMap: { get: (key: string) => (key === 'scheduleId' ? scheduleId : null) } } },
        },
      ],
    });
    fixture = TestBed.createComponent(ScheduleFormPage);
    component = fixture.componentInstance;
    el = fixture.nativeElement;
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  const checkboxFor = (name: string): HTMLInputElement => {
    const label = [...el.querySelectorAll('app-tool-selector [id$="-name"]')].find(
      (n) => n.textContent!.trim() === name,
    )!;
    return el.querySelector(`input[aria-labelledby="${label.id}"]`) as HTMLInputElement;
  };

  afterEach(() => TestBed.resetTestingModule());

  describe('create mode', () => {
    beforeEach(() => mount());

    it('renders one shared selector, named by the Tools label, with the snapshot note', () => {
      const selectors = el.querySelectorAll('app-tool-selector');
      expect(selectors).toHaveLength(1);
      const list = el.querySelector('app-tool-selector [role="group"][aria-labelledby="schedule-tools-label"]')!;
      expect(list).not.toBeNull();
      const helper = el.querySelector(`#${list.getAttribute('aria-describedby')}`)!;
      expect(helper.textContent).toContain('The tool set is snapshotted');
    });

    it('shows descriptions and a search, which the old name-only list lacked', () => {
      expect(el.querySelector('app-tool-selector input[type="search"]')).not.toBeNull();
      expect(el.textContent).toContain('Find classes');
    });

    it('selects a tool through the rendered checkbox', () => {
      checkboxFor('Calculator').click();
      fixture.detectChanges();
      expect([...component.selectedToolIds()]).toEqual(['calculator']);
    });

    it('discloses retirement as visible text and refuses to add the tool', () => {
      expect(checkboxFor('Old Search').disabled).toBe(true);
      expect(el.textContent).toContain(
        'Being retired and can no longer be added to a schedule. Replaced by Class Search.',
      );
    });

    it('drops a retiring tool even if something hands it to the handler', () => {
      component.onToolSelectionChange(new Set(['old_search', 'calculator']));
      expect([...component.selectedToolIds()]).toEqual(['calculator']);
    });
  });

  describe('edit mode', () => {
    beforeEach(() => mount('sched-1'));

    it('makes the picker read-only while "Reset to all my tools" is checked', () => {
      expect(checkboxFor('Calculator').disabled).toBe(false);
      component.onClearToolsChange(true);
      fixture.detectChanges();
      const boxes = [...el.querySelectorAll<HTMLInputElement>('app-tool-selector input[type="checkbox"]')];
      expect(boxes.every((b) => b.disabled)).toBe(true);
      expect(component.selectedToolIds().size).toBe(0);
    });
  });
});
