import { LiveAnnouncer } from '@angular/cdk/a11y';
import { Location } from '@angular/common';
import { Component, input, signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AgentMentionService, MentionableAgent } from '../agents/services/agent-mention.service';
import { RunProgress, RunStepLine } from '../session/services/handoff/workflow-run-progress';
import { WorkflowRunView } from '../session/services/handoff/workflow-run.models';
import { ModelService } from '../session/services/model/model.service';
import { FEATURES } from '../services/features';
import { environment } from '../../environments/environment';
import { fitScale } from './components/workflow-graph.component';
import { WorkflowTestRunPanelComponent } from './components/workflow-test-run-panel.component';
import { WorkflowDesignerPage, elapsedLabel } from './workflow-designer.page';
import { TestRunRecord, WorkflowTestRunService } from './workflow-test-run.service';
import { WorkflowDocument, WorkflowView, WorkflowWrite } from './workflow.models';
import { WorkflowStartService } from '../services/workflows/workflow-start.service';
import { WorkflowsApiService } from '../services/workflows/workflows-api.service';

const AGENTS: MentionableAgent[] = [
  { agentId: 'AST-gen', name: 'Generalist', emoji: '🧭', group: 'own' },
  { agentId: 'AST-fis', name: 'Fiscal reader', emoji: '💵', group: 'pinned' },
  { agentId: 'AST-col', name: 'Colleague', emoji: '🤝', group: 'own' },
];

const AGENT_VIEW = { canRun: true, hasIcon: false, changed: false, runsDraft: false };

function view(overrides: Partial<WorkflowView> = {}): WorkflowView {
  return {
    skillId: 'SKL-1',
    name: 'proposal-review',
    title: 'Proposal review',
    version: 3,
    files: 'one',
    request: 'Review this proposal',
    steps: [
      { name: 'generalist', kind: 'agent', agentId: 'AST-gen', agentVersion: 2, reads: ['request', 'files'], effectiveRequest: 'Review this proposal', agent: { ...AGENT_VIEW, name: 'Generalist', modelId: 'us.model-a', inputPricePerMillionTokens: 3, publishedVersion: 2 } },
      { name: 'fiscal', kind: 'agent', agentId: 'AST-fis', agentVersion: 4, reads: ['request', 'files'], effectiveRequest: 'Review this proposal', agent: { ...AGENT_VIEW, name: 'Fiscal reader', publishedVersion: 4 } },
      { name: 'colleague', kind: 'agent', agentId: 'AST-col', agentVersion: 1, reads: ['request', 'files'], effectiveRequest: 'Review this proposal', agent: { ...AGENT_VIEW, name: 'Colleague', publishedVersion: 1 } },
    ],
    result: { mode: 'card', steps: ['generalist', 'fiscal', 'colleague'] },
    fingerprint: 'f',
    isOwned: true,
    ...overrides,
  };
}

/** The panel stands in for itself: its own spec covers what it renders. */
@Component({ selector: 'app-workflow-test-run-panel', template: '<p data-testid="test-run-panel">{{ ref().name }}</p>' })
class StubTestRunPanel {
  readonly ref = input.required<{ skillId: string; name: string }>();
}

const APPROVED_AT = '2026-11-20T10:00:00Z';

function runLine(stepName: string, agentId: string, state: RunStepLine['state'], updatedAt = APPROVED_AT): RunStepLine {
  const step = { stepId: `st-${stepName}`, handoffId: `h-${stepName}`, agentId, agentName: stepName, request: 'r', removed: false, stepName };
  const row = state === 'queued' ? null : ({ handoffId: step.handoffId, targetSessionId: `t-${stepName}`, updatedAt } as RunStepLine['row']);
  return { step, row, state };
}

/** A stand-in for the test-run service: a record and a run whose progress the spec sets. */
class FakeTestRuns {
  readonly record = signal<TestRunRecord | undefined>(undefined);
  readonly run = signal<WorkflowRunView | undefined>(undefined);
  readonly lines = signal<RunStepLine[]>([]);
  readonly begin = vi.fn(async () => {
    this.record.set({ sessionId: 'test-conv' });
    return true;
  });
  recordFor() {
    return this.record();
  }
  runFor() {
    return this.run();
  }
  progress(): RunProgress {
    const lines = this.lines();
    const done = lines.filter((l) => l.state === 'done').length;
    return { lines, total: lines.length, done, needsInput: 0, settled: false, active: true };
  }
  phrase(line: RunStepLine): string {
    return line.state === 'done' ? 'Finished' : line.state === 'needs_input' ? 'Waiting for your approval to run budget_check' : 'Working';
  }
}

function doc(overrides: Partial<WorkflowView> = {}, written?: boolean): WorkflowDocument {
  return { workflow: view(overrides), sheet: { startable: true, blockers: [] }, written };
}

describe('WorkflowDesignerPage', () => {
  let fixture: ComponentFixture<WorkflowDesignerPage>;
  let el: HTMLElement;
  let service: {
    read: ReturnType<typeof vi.fn>;
    create: ReturnType<typeof vi.fn>;
    write: ReturnType<typeof vi.fn>;
    validate: ReturnType<typeof vi.fn>;
    remove: ReturnType<typeof vi.fn>;
    messageFor: (err: unknown, fallback: string) => string;
  };
  let announce: ReturnType<typeof vi.fn>;
  let replaceState: ReturnType<typeof vi.fn>;
  let start: ReturnType<typeof vi.fn>;
  let testRuns: FakeTestRuns;

  beforeEach(() => {
    testRuns = new FakeTestRuns();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    service = {
      read: vi.fn().mockResolvedValue({ ok: true, document: doc() }),
      create: vi.fn(),
      write: vi.fn(),
      validate: vi.fn().mockResolvedValue(doc()),
      remove: vi.fn(),
      messageFor: (_err, fallback) => fallback,
    };
    announce = vi.fn();
    replaceState = vi.fn();
    start = vi.fn().mockResolvedValue('new-session');
    TestBed.configureTestingModule({
      imports: [WorkflowDesignerPage],
      providers: [
        provideRouter([]),
        { provide: WorkflowsApiService, useValue: service },
        { provide: AgentMentionService, useValue: { mentionable: signal(AGENTS), load: vi.fn().mockResolvedValue(undefined) } },
        { provide: ModelService, useValue: { modelNameFor: (id: string) => (id === 'us.model-a' ? 'Model A' : null) } },
        { provide: LiveAnnouncer, useValue: { announce } },
        { provide: Location, useValue: { replaceState, path: () => '' } },
        { provide: WorkflowStartService, useValue: { start } },
        { provide: WorkflowTestRunService, useValue: testRuns },
      ],
    });
    TestBed.overrideComponent(WorkflowDesignerPage, {
      remove: { imports: [WorkflowTestRunPanelComponent] },
      add: { imports: [StubTestRunPanel] },
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    fixture?.destroy();
  });

  async function settle(): Promise<void> {
    for (let i = 0; i < 4; i++) {
      await new Promise((resolve) => setTimeout(resolve));
      await fixture.whenStable();
      fixture.detectChanges();
    }
  }

  async function open(inputs: { skillId?: string; name?: string; template?: string }): Promise<void> {
    fixture = TestBed.createComponent(WorkflowDesignerPage);
    for (const [key, value] of Object.entries(inputs)) fixture.componentRef.setInput(key, value);
    el = fixture.nativeElement as HTMLElement;
    fixture.detectChanges();
    await settle();
  }

  function button(name: string): HTMLButtonElement {
    const found = [...el.querySelectorAll<HTMLButtonElement>('button')].find(
      (b) => (b.getAttribute('aria-label') ?? b.textContent ?? '').trim().startsWith(name),
    );
    if (!found) throw new Error(`no button "${name}"`);
    return found;
  }

  describe('the canvas', () => {
    it('is a list of stages, each a labelled group of step buttons whose names say what the card shows', async () => {
      await open({ skillId: 'SKL-1', name: 'proposal-review' });

      const group = el.querySelector('[role="group"]')!;
      expect(el.querySelector(`#${group.getAttribute('aria-labelledby')}`)!.textContent).toContain('Stage 1');
      const cards = [...group.querySelectorAll('ul[role="list"] button')];
      expect(cards.map((c) => c.getAttribute('aria-label'))).toEqual([
        'Generalist, Agent step, stage 1, reads the request and the files',
        'Fiscal reader, Agent step, stage 1, reads the request and the files',
        'Colleague, Agent step, stage 1, reads the request and the files',
      ]);
      expect(cards[0].textContent).toContain('Agent · Model A');
      expect(el.textContent).toContain('3 of 8 steps');
    });

    it('hides the connectors from assistive technology', async () => {
      await open({ skillId: 'SKL-1', name: 'proposal-review' });
      expect(el.querySelector('app-workflow-graph svg')!.getAttribute('aria-hidden')).toBe('true');
    });

    it('turns Add step off at the cap, and says so from the start', async () => {
      const steps = Array.from({ length: 8 }, (_, i) => ({ ...view().steps[0], name: `s${i}`, agentId: `AST-${i}` }));
      service.read.mockResolvedValue({ ok: true, document: doc({ steps }) });
      await open({ skillId: 'SKL-1', name: 'proposal-review' });
      expect(button('Add step').disabled).toBe(true);
      expect(el.textContent).toContain('8 of 8 steps');
    });

    it('offers no Share', async () => {
      await open({ skillId: 'SKL-1', name: 'proposal-review' });
      expect([...el.querySelectorAll('button')].some((b) => /share/i.test(b.textContent ?? ''))).toBe(false);
    });
  });

  describe('stages', () => {
    /** Colleague reads Generalist and Fiscal reader, so it is stage 2; the Result reads Colleague only. */
    function staged(): WorkflowDocument {
      const [gen, fis, col] = view().steps;
      return doc({
        steps: [gen, fis, { ...col, reads: ['request', 'files', 'generalist', 'fiscal'] }],
        result: { mode: 'merge', reads: ['colleague'], steps: ['colleague'], instruction: 'Write one report.' },
      });
    }

    function checkbox(label: string): HTMLInputElement {
      const found = [...el.querySelectorAll<HTMLLabelElement>('#wf-panel-step label')].find((l) => l.textContent!.includes(label));
      if (!found) throw new Error(`no checkbox "${label}"`);
      return found.querySelector('input')!;
    }

    it('draws a column per stage from the reads, with connectors that follow them', async () => {
      service.read.mockResolvedValue({ ok: true, document: staged() });
      await open({ skillId: 'SKL-1', name: 'proposal-review' });

      const groups = [...el.querySelectorAll('[role="group"]')];
      const headings = groups.map((g) => el.querySelector(`#${g.getAttribute('aria-labelledby')}`)!.textContent!.trim());
      expect(headings).toEqual(['Stage 1', 'Stage 2', 'Stage 3']);
      expect([...groups[1].querySelectorAll('ul button')].map((c) => c.getAttribute('aria-label'))).toEqual([
        'Colleague, Agent step, stage 2, reads the request, the files and 2 results',
      ]);
      const edges = fixture.componentInstance['graphEdges']().map(([from, to]) => `${from}>${to}`);
      const keys = fixture.componentInstance['graphStages']().flat().map((c) => c.key);
      const [gen, fis, col] = keys;
      expect(edges).toEqual([`start>${gen}`, `start>${fis}`, `${gen}>${col}`, `${fis}>${col}`, `${col}>result`]);
    });

    it('counts the results the Result reads, not every step, and names a merge', async () => {
      service.read.mockResolvedValue({ ok: true, document: staged() });
      await open({ skillId: 'SKL-1', name: 'proposal-review' });
      const result = el.querySelector('[data-node-id="result"]')!;
      expect(result.textContent).toContain('A merge');
      expect(result.textContent).toContain('Starts by itself · 1 result');
    });

    it('starts a new stage from the last column, reading every step of the stage before', async () => {
      await open({ skillId: 'SKL-1', name: 'proposal-review' });
      button('Add step to stage 2, a new stage').click();
      await settle();

      expect(announce).toHaveBeenCalledWith('Step added to stage 2. 4 of 8 steps', 'polite');
      expect(checkbox('Generalist’s result').checked).toBe(true);
      expect(checkbox('Fiscal reader’s result').checked).toBe(true);
      expect(checkbox('Colleague’s result').checked).toBe(true);
      expect(el.querySelector('#wf-panel-step')!.textContent).toContain('Agent step · stage 2');
    });

    it('reads another step, says the new stage, and keeps focus on the checkbox', async () => {
      await open({ skillId: 'SKL-1', name: 'proposal-review' });
      button('Colleague').click();
      await settle();

      const box = checkbox('Generalist’s result');
      expect(box.disabled).toBe(false);
      box.focus();
      box.click();
      await settle();

      expect(announce).toHaveBeenCalledWith('Colleague moved to stage 2', 'polite');
      expect(document.activeElement).toBe(checkbox('Generalist’s result'));
      expect(el.querySelector('#wf-panel-step')!.textContent).toContain('Agent step · stage 2');
      expect(el.querySelector('#wf-reads-rules')!.textContent).toContain('can’t read itself, or a step that reads it');
      expect(el.querySelector('#wf-reads-rules')!.textContent).toContain('3 of 8 steps');
    });

    it('never offers a read that would make a cycle, and says why', async () => {
      service.read.mockResolvedValue({ ok: true, document: staged() });
      await open({ skillId: 'SKL-1', name: 'proposal-review' });
      button('Generalist').click();
      await settle();

      const box = checkbox('Colleague’s result');
      expect(box.disabled).toBe(true);
      const why = el.querySelector(`#${box.getAttribute('aria-describedby')}`)!;
      expect(why.textContent).toContain('Reads this step');
      // The step itself is never listed.
      expect([...el.querySelectorAll('#wf-panel-step label')].some((l) => l.textContent!.includes('Generalist’s result'))).toBe(false);
    });

    it('edits the Result: merge and its instruction, and which results it includes, and saves them', async () => {
      await open({ skillId: 'SKL-1', name: 'proposal-review' });
      button('Result').click();
      await settle();

      const merge = el.querySelector<HTMLInputElement>('input[name="wf-result"][value="merge"]')!;
      expect(merge.disabled).toBe(false);
      merge.click();
      await settle();
      expect(document.activeElement?.id).toBe('wf-result-instruction');
      const instruction = el.querySelector<HTMLTextAreaElement>('#wf-result-instruction')!;
      instruction.value = 'Write one report.';
      instruction.dispatchEvent(new Event('input'));
      checkbox('Generalist’s result').click();
      checkbox('Fiscal reader’s result').click();
      await settle();

      // One left, and it can't be left out.
      expect(checkbox('Colleague’s result').disabled).toBe(true);
      expect(el.querySelector('[data-node-id="result"]')!.textContent).toContain('Starts by itself · 1 result');

      service.write.mockResolvedValue({ ok: true, document: doc({ version: 4 }, true) });
      button('Save workflow').click();
      await settle();
      expect((service.write.mock.calls[0][1] as WorkflowWrite).result).toEqual({
        mode: 'merge',
        reads: ['colleague'],
        instruction: 'Write one report.',
      });
    });

    it('shows the server’s message when it refuses a save', async () => {
      await open({ skillId: 'SKL-1', name: 'proposal-review' });
      const title = el.querySelector<HTMLInputElement>('#wf-title')!;
      title.value = 'Edited';
      title.dispatchEvent(new Event('input'));
      await settle();
      service.write.mockResolvedValue({
        ok: false,
        conflict: false,
        message: "workflow 'proposal-review' has a cycle: generalist → colleague → generalist",
      });
      button('Save workflow').click();
      await settle();
      expect(el.querySelector('header [role="alert"]')!.textContent).toContain('has a cycle');
    });
  });

  describe('Move up and Move down', () => {
    it('moves the step, keeps focus on the control, and says where it went', async () => {
      await open({ skillId: 'SKL-1', name: 'proposal-review' });
      button('Fiscal reader').click();
      await settle();

      const down = button('Move Fiscal reader down');
      down.focus();
      down.click();
      await settle();

      const names = [...el.querySelectorAll('[role="group"] ul[role="list"] button')].map((c) => c.getAttribute('aria-label')!.split(',')[0]);
      expect(names).toEqual(['Generalist', 'Colleague', 'Fiscal reader']);
      expect(announce).toHaveBeenCalledWith('Fiscal reader moved to position 3 of 3 in stage 1', 'polite');
      // At the bottom Move down is off, so focus lands on Move up.
      expect(document.activeElement?.id).toBe('wf-move-up');
      expect(el.textContent).toContain('Unsaved changes');
    });
  });

  describe('a new workflow from the template', () => {
    it('saves nothing until every step has an Agent', async () => {
      await open({ template: 'proposal-review' });
      expect(el.textContent).toContain('Choose an Agent for the 4 steps that have none.');
      expect((el.querySelector('header button:last-of-type') as HTMLButtonElement).disabled).toBe(true);
      expect(service.create).not.toHaveBeenCalled();
      expect(service.validate).not.toHaveBeenCalled();
    });
  });

  describe('a new blank workflow', () => {
    it('creates the carrier, writes against the version it was created at, and takes its URL without navigating', async () => {
      await open({});
      button('Add step').click();
      await settle();
      const radio = [...el.querySelectorAll<HTMLInputElement>('input[name="wf-agent"]')].find((r) => r.value === 'AST-gen')!;
      radio.click();
      await settle();

      service.create.mockResolvedValue(doc({ version: 1, steps: [], name: 'untitled-workflow' }));
      service.validate.mockResolvedValue(doc({ version: 1, name: 'untitled-workflow', steps: [view().steps[0]] }));
      service.write.mockResolvedValue({ ok: true, document: doc({ version: 2, name: 'untitled-workflow', steps: [view().steps[0]] }, true) });

      button('Save workflow').click();
      await settle();

      expect(service.create).toHaveBeenCalledWith({ title: 'Untitled workflow' });
      const body = service.write.mock.calls[0][1] as WorkflowWrite;
      expect(body.expectedVersion).toBe(1);
      expect(body.steps).toEqual([
        { name: 'generalist', kind: 'agent', agentId: 'AST-gen', agentVersion: 2, reads: ['request', 'files'], request: null },
      ]);
      expect(replaceState).toHaveBeenCalledWith('/workflows/SKL-1/untitled-workflow');
      expect(el.textContent).toContain('Saved · version 2');
    });
  });

  describe('saving', () => {
    it('sends the version it opened, and a conflict offers to reload what is stored', async () => {
      await open({ skillId: 'SKL-1', name: 'proposal-review' });
      const title = el.querySelector<HTMLInputElement>('#wf-title')!;
      title.value = 'Proposal review, edited';
      title.dispatchEvent(new Event('input'));
      await settle();

      service.write.mockResolvedValue({
        ok: false,
        conflict: true,
        message: 'This workflow changed somewhere else since you opened it.',
        document: doc({ version: 5, title: 'Changed elsewhere' }),
      });
      button('Save workflow').click();
      await settle();

      expect((service.write.mock.calls[0][1] as WorkflowWrite).expectedVersion).toBe(3);
      const alert = el.querySelector('[role="alert"]')!;
      expect(alert.textContent).toContain('changed somewhere else');

      button('Reload the saved workflow').click();
      await settle();
      expect(el.querySelector<HTMLInputElement>('#wf-title')!.value).toBe('Changed elsewhere');
      expect(el.textContent).toContain('Saved · version 5');
    });
  });

  describe('the Preview tab', () => {
    it('is the server’s sheet for the draft, with its blockers, and makes no other call', async () => {
      service.validate.mockResolvedValue({
        ...doc(),
        sheet: { startable: false, blockers: [{ code: 'step_unavailable', step: 'fiscal', message: 'You can’t use this step’s Agent' }] },
      });
      await open({ skillId: 'SKL-1', name: 'proposal-review' });
      // An edit makes the draft differ from what GET described, so the sheet is asked for again.
      el.querySelector<HTMLInputElement>('#wf-title')!.dispatchEvent(new Event('input'));
      const title = el.querySelector<HTMLInputElement>('#wf-title')!;
      title.value = 'Edited';
      title.dispatchEvent(new Event('input'));
      await vi.advanceTimersByTimeAsync(600);
      await settle();

      el.querySelector<HTMLButtonElement>('#wf-tab-preview')!.click();
      await settle();

      expect(service.validate).toHaveBeenCalledTimes(1);
      const panel = el.querySelector('#wf-panel-preview')!;
      expect(panel.textContent).toContain('Asks for exactly one file');
      expect(panel.textContent).toContain('Model A');
      expect(panel.textContent).toContain('Fiscal reader: You can’t use this step’s Agent');
      expect(panel.textContent).toContain('makes no model call');
      expect(service.write).not.toHaveBeenCalled();
    });

    it('Try in a chat starts the saved workflow through D.2’s start service, which navigates', async () => {
      await open({ skillId: 'SKL-1', name: 'proposal-review' });
      el.querySelector<HTMLButtonElement>('#wf-tab-preview')!.click();
      await settle();

      button('Try in a chat').click();
      await settle();

      expect(start.mock.calls).toEqual([[{ skillId: 'SKL-1', name: 'proposal-review' }]]);
      expect(testRuns.begin).not.toHaveBeenCalled();
      expect(service.validate).not.toHaveBeenCalled();
    });

    it('Start test run opens the sheet in the panel’s Test run tab and stays on the designer', async () => {
      await open({ skillId: 'SKL-1', name: 'proposal-review' });
      expect(el.querySelector('#wf-tab-test')).toBeNull();
      el.querySelector<HTMLButtonElement>('#wf-tab-preview')!.click();
      await settle();

      button('Start test run').click();
      await settle();

      expect(testRuns.begin).toHaveBeenCalledWith({ skillId: 'SKL-1', name: 'proposal-review' });
      expect(start).not.toHaveBeenCalled();
      const tab = el.querySelector<HTMLButtonElement>('#wf-tab-test')!;
      expect(tab.getAttribute('aria-selected')).toBe('true');
      expect(el.querySelector('#wf-panel-test [data-testid="test-run-panel"]')!.textContent).toBe('proposal-review');
      // The sheet is up but nothing has started: the canvas is still the designer's.
      expect(el.querySelector('[aria-label="Workflow canvas"]')).not.toBeNull();
    });

    it('keeps Start off while there are unsaved changes, and says why', async () => {
      await open({ skillId: 'SKL-1', name: 'proposal-review' });
      const title = el.querySelector<HTMLInputElement>('#wf-title')!;
      title.value = 'Edited';
      title.dispatchEvent(new Event('input'));
      el.querySelector<HTMLButtonElement>('#wf-tab-preview')!.click();
      await settle();

      expect(button('Start test run').disabled).toBe(true);
      expect(button('Try in a chat').disabled).toBe(true);
      expect(el.querySelector('#wf-start-blocked')!.textContent).toContain('Save your changes to start');
    });
  });

  describe('the test run', () => {
    function approvedRun(): WorkflowRunView {
      return { runId: 'RUN-1', sessionId: 'test-conv', status: 'approved', approvedAt: APPROVED_AT } as WorkflowRunView;
    }

    it('comes back on its own tab, and the canvas draws the run: states, phrases, elapsed, nothing to edit', async () => {
      testRuns.record.set({ sessionId: 'test-conv', runId: 'RUN-1' });
      testRuns.run.set(approvedRun());
      testRuns.lines.set([
        runLine('generalist', 'AST-gen', 'done', '2026-11-20T10:01:04Z'),
        runLine('fiscal', 'AST-fis', 'needs_input'),
        runLine('colleague', 'AST-col', 'running'),
      ]);
      await open({ skillId: 'SKL-1', name: 'proposal-review' });

      expect(el.querySelector('#wf-tab-test')!.getAttribute('aria-selected')).toBe('true');
      expect(el.querySelector('[aria-label="Test run canvas"]')).not.toBeNull();
      const cards = [...el.querySelectorAll<HTMLElement>('[role="group"] [data-node-id]')];
      expect(cards.map((c) => c.dataset['state'])).toEqual(['done', 'needs_input', 'running']);
      expect(cards.map((c) => c.textContent!.replace(/\s+/g, ' ').trim())).toEqual([
        '🧭Generalist Finished · 1m 04s',
        '💵Fiscal reader Waiting for your approval to run budget_check',
        '🤝Colleague Working',
      ]);
      // Nothing on the canvas edits a run: no card is a button, and Add step is gone.
      expect(el.querySelector('[role="group"] button')).toBeNull();
      expect(el.textContent).not.toContain('Add step');
      // The running pulse stops under reduced motion.
      expect(el.querySelector('[data-mark="running"] .animate-ping')!.classList).toContain('motion-reduce:animate-none');
      expect(el.querySelector('[data-node-id="result"]')!.textContent).toContain('1 of 3 finished');
    });

    it('draws the canvas for design again on the Step tab', async () => {
      testRuns.record.set({ sessionId: 'test-conv', runId: 'RUN-1' });
      testRuns.run.set(approvedRun());
      testRuns.lines.set([runLine('generalist', 'AST-gen', 'running')]);
      await open({ skillId: 'SKL-1', name: 'proposal-review' });

      el.querySelector<HTMLButtonElement>('#wf-tab-step')!.click();
      await settle();

      expect(el.querySelector('[aria-label="Workflow canvas"]')).not.toBeNull();
      expect(el.querySelectorAll('[role="group"] ul button').length).toBe(3);
      expect(el.textContent).toContain('Add step');
    });

    it('goes back to Preview when the test run is cancelled on its sheet', async () => {
      testRuns.record.set({ sessionId: 'test-conv' });
      await open({ skillId: 'SKL-1', name: 'proposal-review' });
      expect(el.querySelector('#wf-tab-test')!.getAttribute('aria-selected')).toBe('true');

      testRuns.record.set(undefined);
      await settle();

      expect(el.querySelector('#wf-tab-test')).toBeNull();
      expect(el.querySelector('#wf-tab-preview')!.getAttribute('aria-selected')).toBe('true');
    });

    it('moves between three tabs with the arrow keys, wrapping', async () => {
      testRuns.record.set({ sessionId: 'test-conv' });
      await open({ skillId: 'SKL-1', name: 'proposal-review' });

      const test = el.querySelector<HTMLButtonElement>('#wf-tab-test')!;
      test.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }));
      await settle();
      expect(el.querySelector('#wf-tab-step')!.getAttribute('aria-selected')).toBe('true');
      el.querySelector('#wf-tab-step')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft' }));
      await settle();
      expect(el.querySelector('#wf-tab-test')!.getAttribute('aria-selected')).toBe('true');
    });
  });

  describe('prompt steps (PR-D.6c)', () => {
    const MODELS = [
      { modelId: 'us.model-a', modelName: 'Model A', provider: 'bedrock', inputModalities: ['TEXT', 'IMAGE'], outputModalities: ['TEXT'] },
      { modelId: 'us.micro', modelName: 'Micro', provider: 'bedrock', inputModalities: ['TEXT'], outputModalities: ['TEXT'] },
      { modelId: 'us.sonic', modelName: 'Sonic', provider: 'bedrock', inputModalities: ['TEXT', 'SPEECH'], outputModalities: ['SPEECH'] },
    ];

    function on(promptSteps: boolean): void {
      TestBed.overrideProvider(FEATURES, { useValue: { ...environment.features, promptSteps } });
      TestBed.overrideProvider(ModelService, {
        useValue: {
          modelNameFor: (id: string) => MODELS.find((m) => m.modelId === id)?.modelName ?? null,
          availableModels: signal(MODELS),
        },
      });
    }

    function radio(label: string): HTMLInputElement {
      const found = [...el.querySelectorAll('label')].find((l) => (l.textContent ?? '').trim().startsWith(label))?.querySelector('input');
      if (!found) throw new Error(`no radio "${label}"`);
      return found as HTMLInputElement;
    }

    it('offers no prompt step while the flag is off', async () => {
      on(false);
      await open({ skillId: 'SKL-1', name: 'proposal-review' });
      button('Add step to stage 1').click();
      await settle();
      expect(el.querySelector('#wf-kind-prompt')).toBeNull();
    });

    it('Add step offers Prompt; the step gets a model the user can use, a name and an instruction, and saves as a prompt step', async () => {
      on(true);
      await open({ skillId: 'SKL-1', name: 'proposal-review' });
      button('Add step to stage 1').click();
      await settle();
      expect(document.activeElement?.id).toBe('wf-kind-agent');

      (el.querySelector('#wf-kind-prompt') as HTMLInputElement).click();
      await settle();
      // Only models that run a prompt step: the speech model is left out.
      const models = [...el.querySelectorAll<HTMLInputElement>('input[name="wf-model"]')].map((input) => input.value);
      expect(models).toEqual(['us.model-a', 'us.micro']);
      // A text-only model can't be sent the run's files, which a new step reads.
      expect(radio('Micro').disabled).toBe(true);
      expect(el.textContent).toContain('Choose the model this step runs on.');
      expect(button('Save workflow').disabled).toBe(true);
      expect(el.textContent).toContain('Choose a model for the prompt step that has none.');

      radio('Model A').click();
      const name = el.querySelector('#wf-step-name') as HTMLInputElement;
      name.value = 'Budget check';
      name.dispatchEvent(new Event('change'));
      const instruction = el.querySelector('#wf-instruction') as HTMLTextAreaElement;
      instruction.value = 'List the budget risks.';
      instruction.dispatchEvent(new Event('input'));
      await settle();

      const card = el.querySelector('[data-node-id][aria-pressed="true"]')!;
      expect(card.getAttribute('aria-label')).toBe('Budget check, Prompt · Model A, stage 1, reads the request and the files');
      expect(card.textContent).toContain('Prompt · Model A');

      service.write.mockResolvedValue({ ok: true, document: doc({ version: 4 }, true) });
      button('Save workflow').click();
      await settle();
      const body = service.write.mock.calls[0][1] as WorkflowWrite;
      expect(body.steps[body.steps.length - 1]).toEqual({
        name: 'budget_check',
        kind: 'prompt',
        agentId: '',
        modelId: 'us.model-a',
        reads: ['request', 'files'],
        request: 'List the budget risks.',
      });
    });
  });
});

describe('elapsedLabel', () => {
  it('reads seconds, then minutes and seconds, then hours and minutes', () => {
    expect(elapsedLabel('2026-11-20T10:00:00Z', '2026-11-20T10:00:42Z')).toBe('42s');
    expect(elapsedLabel('2026-11-20T10:00:00Z', '2026-11-20T10:01:04Z')).toBe('1m 04s');
    expect(elapsedLabel('2026-11-20T10:00:00Z', '2026-11-20T11:02:00Z')).toBe('1h 02m');
    expect(elapsedLabel(undefined, '2026-11-20T10:00:42Z')).toBeUndefined();
    expect(elapsedLabel('2026-11-20T10:00:42Z', '2026-11-20T10:00:00Z')).toBeUndefined();
  });
});

describe('fitScale', () => {
  it('scales the graph to the canvas but never below 60% or above actual size', () => {
    expect(fitScale({ width: 2300, height: 500 }, { width: 900, height: 600 })).toBe(0.6);
    expect(fitScale({ width: 1000, height: 500 }, { width: 900, height: 600 })).toBe(0.9);
    expect(fitScale({ width: 600, height: 400 }, { width: 900, height: 600 })).toBe(1);
  });
});
