// A workflow run's row in its source thread (workflow-runs spec §20.2, PR-W.4b). The services are
// stubbed; the run's progress rules are pinned in workflow-run-progress.spec.ts and the actions'
// HTTP in workflow-run.service.spec.ts / workflow-run-result.service.spec.ts.
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { MarkdownService, provideMarkdown } from 'ngx-markdown';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { FEATURES } from '../../../services/features';
import { UserConnectorsService } from '../../../settings/connectors/services/user-connectors.service';
import { WorkflowsApiService } from '../../../services/workflows/workflows-api.service';
import { ChatStateService } from '../../services/chat/chat-state.service';
import { ToolInsightService } from '../../services/chat/tool-insight.service';
import { AgentHandoffService } from '../../services/handoff/agent-handoff.service';
import { HandoffRelayService } from '../../services/handoff/handoff-relay.service';
import { HandoffStateService } from '../../services/handoff/handoff-state.service';
import { HandoffStatus, HandoffView } from '../../services/handoff/handoff.models';
import { WorkflowRunQueueService } from '../../services/handoff/workflow-run-queue.service';
import { WorkflowRunMergeService } from '../../services/handoff/workflow-run-merge.service';
import { WorkflowRunResultService } from '../../services/handoff/workflow-run-result.service';
import { WorkflowRunView } from '../../services/handoff/workflow-run.models';
import { WorkflowRunService } from '../../services/handoff/workflow-run.service';
import { SessionService } from '../../services/session/session.service';
import { WorkflowRunRowComponent } from './workflow-run-row.component';

const NAMES = ['Generalist', 'Colleague', 'Emotional outsider', 'Fiscal outsider'];

function text(el: Element | null): string {
  return (el?.textContent ?? '').replace(/\s+/g, ' ').trim();
}

function stepRow(i: number, status: HandoffStatus, extra: Partial<HandoffView> = {}): HandoffView {
  return {
    handoffId: `h${i + 1}`,
    sessionId: 'src',
    kind: 'mention',
    status,
    agentId: `a${i + 1}`,
    agentName: NAMES[i],
    request: 'review this proposal',
    sourceMessageIndex: 1,
    excerptTokens: 10,
    createdAt: '',
    updatedAt: '',
    runId: 'run1',
    stepId: `s${i + 1}`,
    ...(status === 'open' ? {} : { targetSessionId: `t${i + 1}` }),
    ...extra,
  };
}

function run(rows: HandoffView[], overrides: Partial<WorkflowRunView> = {}): WorkflowRunView {
  return {
    runId: 'run1',
    sessionId: 'src',
    status: 'approved',
    request: 'review this proposal',
    steps: rows.map((row, i) => ({
      stepId: `s${i + 1}`,
      handoffId: row.handoffId,
      agentId: row.agentId,
      agentName: row.agentName,
      request: row.request,
      removed: false,
      handoff: row,
    })),
    fileIds: [],
    limits: { maxSteps: 8, concurrency: 4 },
    sourceMessageIndex: 1,
    createdAt: '',
    updatedAt: '',
    card: { text: 'card', removed: false },
    ...overrides,
  };
}

describe('WorkflowRunRowComponent', () => {
  const pending = new Set<string>();
  const loading = new Set<string>();
  const promptRunning = new Set<string>();
  const onComposer = signal(false);
  const runs = { resume: vi.fn(), stop: vi.fn() };
  const results = { collect: vi.fn(), isCollecting: () => false, isOnComposer: () => onComposer(), resultCardOf: () => 'd1' };
  const mergeHold = signal<string | null>('not_on_screen');
  const merge = { holdFor: vi.fn(() => mergeHold()), startNow: vi.fn() };
  const features = { workflows: false };
  let sessionTitle = 'Proposal review';
  const workflows = { create: vi.fn(), messageFor: (_err: unknown, fallback: string) => fallback };

  beforeEach(() => {
    pending.clear();
    loading.clear();
    promptRunning.clear();
    onComposer.set(false);
    runs.resume.mockClear();
    runs.stop.mockClear();
    results.collect.mockClear();
    merge.startNow.mockClear();
    mergeHold.set('not_on_screen');
    workflows.create.mockReset();
    features.workflows = false;
    sessionTitle = 'Proposal review';
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        provideMarkdown(),
        { provide: FEATURES, useValue: features },
        { provide: WorkflowsApiService, useValue: workflows },
        {
          provide: AgentHandoffService,
          useValue: { targetLink: (row: HandoffView) => (row.targetSessionId ? { commands: ['/s', row.targetSessionId], queryParams: {} } : null) },
        },
        { provide: ChatStateService, useValue: { isSessionLoading: (id: string) => loading.has(id) } },
        { provide: ToolInsightService, useValue: { latestSummary: () => undefined, status: () => undefined } },
        {
          provide: SessionService,
          useValue: { mergedSessionsResource: () => ({ sessions: [{ sessionId: 'src', title: sessionTitle }] }) },
        },
        { provide: UserConnectorsService, useValue: { ensureLoaded: vi.fn(), displayNameFor: (id: string) => id } },
        {
          provide: WorkflowRunQueueService,
          useValue: { isPending: (_run: string, id: string) => pending.has(id), isRunning: (_run: string, id: string) => promptRunning.has(id) },
        },
        { provide: WorkflowRunService, useValue: runs },
        { provide: WorkflowRunResultService, useValue: results },
        { provide: WorkflowRunMergeService, useValue: merge },
        {
          provide: HandoffRelayService,
          useValue: {
            notice: () => null,
            isWaiting: () => false,
            consentFor: () => null,
            approvalFor: () => null,
            questionFor: () => null,
            clearNotice: vi.fn(),
            markShown: vi.fn(),
          },
        },
      ],
    });
  });

  function render(
    value: WorkflowRunView,
    inputs: { offerSaveAsWorkflow?: boolean } = {},
  ): { el: HTMLElement; detect: (next?: WorkflowRunView) => void } {
    const fixture = TestBed.createComponent(WorkflowRunRowComponent);
    fixture.componentRef.setInput('run', value);
    if (inputs.offerSaveAsWorkflow !== undefined) fixture.componentRef.setInput('offerSaveAsWorkflow', inputs.offerSaveAsWorkflow);
    fixture.detectChanges();
    return {
      el: fixture.nativeElement as HTMLElement,
      detect: (next) => {
        if (next) fixture.componentRef.setInput('run', next);
        fixture.detectChanges();
      },
    };
  }

  function buttons(el: HTMLElement): string[] {
    return [...el.querySelectorAll('button')].map((b) => text(b));
  }

  it('is a section named by its heading, with a line per step and a bar hidden from assistive tech', () => {
    const rows = [
      stepRow(0, 'done'),
      stepRow(1, 'done'),
      stepRow(2, 'running'),
      stepRow(3, 'needs_input', { waitingOn: { kind: 'tool_approval', name: 'budget_check' } }),
    ];
    const { el } = render(run(rows));

    const section = el.querySelector('section')!;
    const heading = el.querySelector(`#${section.getAttribute('aria-labelledby')}`);
    expect(text(heading)).toBe('Proposal review · 4 Agents, 1 needs you');
    expect(section.getAttribute('aria-live')).toBeNull();
    const bar = el.querySelector('[data-testid="run-bar"]')!;
    expect(bar.getAttribute('aria-hidden')).toBe('true');
    expect([...bar.children].map((c) => c.getAttribute('data-state'))).toEqual(['done', 'done', 'running', 'needs_input']);

    const links = [...el.querySelectorAll('a')];
    expect(links.length).toBe(4);
    expect(text(links[0])).toBe('Generalist · Finished');
    expect(text(links[3])).toBe('Fiscal outsider · Waiting for your approval to run budget_check');
    expect(links[3].getAttribute('aria-label')).toBe('review this proposal, Fiscal outsider, Waiting for your approval to run budget_check');
  });

  it('counts finished steps once nothing needs the user, and a queued step links nowhere', () => {
    pending.add('h4');
    const rows = [stepRow(0, 'done'), stepRow(1, 'running'), stepRow(2, 'running'), stepRow(3, 'open')];
    const { el } = render(run(rows));

    expect(text(el.querySelector('h3'))).toContain('1 of 4 finished');
    expect(el.querySelectorAll('a').length).toBe(3);
    expect(text(el.querySelector('[data-handoff-id="h4"]'))).toBe('Fiscal outsider · Queued');
    expect(el.querySelector('[data-handoff-id="h4"]')!.tagName).toBe('DIV');
  });

  it('offers a partial result and Stop run as siblings under the card, outside every link', () => {
    const rows = [stepRow(0, 'done'), stepRow(1, 'done'), stepRow(2, 'running'), stepRow(3, 'running')];
    const { el } = render(run(rows));

    expect(buttons(el)).toEqual(['Bring back 2 results now', 'Stop run']);
    for (const b of el.querySelectorAll('button')) expect(b.closest('a, section')).toBeNull();
    el.querySelectorAll('button')[0].click();
    expect(results.collect).toHaveBeenCalled();
    el.querySelectorAll('button')[1].click();
    expect(runs.stop).toHaveBeenCalled();
  });

  it('says Bring back results once every step has settled, and no Stop run', () => {
    const rows = [stepRow(0, 'done'), stepRow(1, 'stopped')];
    const { el } = render(run(rows));
    expect(buttons(el)).toEqual(['Bring back results']);
  });

  it('shows the result as added once it waits on the composer, and offers no second one', () => {
    onComposer.set(true);
    const rows = [stepRow(0, 'done'), stepRow(1, 'done')];
    const { el } = render(run(rows, { resultDeliveryId: 'd1' }));
    expect(buttons(el)).toEqual([]);
    expect(text(el.querySelector('[role="status"]'))).toBe('Added to your next message');
  });

  it('offers Start for steps a reload left unstarted, from the card on the run row', () => {
    const rows = [stepRow(0, 'done'), stepRow(1, 'open'), stepRow(2, 'open')];
    const { el } = render(run(rows));

    expect(text(el.querySelector('[data-handoff-id="h2"]'))).toBe('Colleague · Not started');
    expect(buttons(el)[0]).toBe('Start 2 Agents');
    el.querySelectorAll('button')[0].click();
    expect(runs.resume).toHaveBeenCalled();
  });

  it('offers no Start for a run approved before its card was kept, and says why', () => {
    const rows = [stepRow(0, 'open'), stepRow(1, 'open')];
    const { el } = render(run(rows, { card: undefined }));
    expect(buttons(el)).toEqual(['Stop run']);
    expect(text(el)).toContain('These weren’t started.');
  });

  it('a stopped run reads its unstarted steps as not started, and offers no Stop run', () => {
    const rows = [stepRow(0, 'done'), stepRow(1, 'stopped', { stoppedReason: 'user_stopped' }), stepRow(2, 'open')];
    const { el } = render(run(rows, { status: 'stopped' }));
    expect(text(el.querySelector('[data-handoff-id="h3"]'))).toBe('Emotional outsider · Didn’t start');
    expect(text(el.querySelector('a[data-handoff-id="h2"]'))).toBe('Colleague · Stopped');
    expect(buttons(el)).toEqual(['Bring back results']);
  });

  it('a step streaming in this tab reads running whatever its row says', () => {
    loading.add('t1');
    const { el } = render(run([stepRow(0, 'done'), stepRow(1, 'done')]));
    expect(text(el.querySelector('a[data-handoff-id="h1"]'))).toContain('Generalist · Working on');
  });

  it('takes a focus request for one of its steps and focuses that step’s line', () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    const { el, detect } = render(run([stepRow(0, 'done'), stepRow(1, 'needs_input')]));
    TestBed.inject(HandoffStateService).requestFocus('h2');
    detect();
    TestBed.tick();
    expect(scroll).toHaveBeenCalled();
    expect(document.activeElement).toBe(el.querySelector('a[data-handoff-id="h2"]'));
    expect(TestBed.inject(HandoffStateService).focusedHandoffId()).toBeNull();
  });
  describe('Save as workflow (workflow-designer §8.2)', () => {
    it('is offered on a finished run while workflows are on, and saves it and opens the designer', async () => {
      features.workflows = true;
      workflows.create.mockResolvedValue({ workflow: { skillId: 'SKL-1', name: 'proposal-review' } });
      const { el } = render(run([stepRow(0, 'done'), stepRow(1, 'stopped')]));
      const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);

      expect(buttons(el)).toEqual(['Bring back results', 'View as graph', 'Save as workflow']);
      [...el.querySelectorAll('button')].find((b) => text(b) === 'Save as workflow')!.click();
      await new Promise((resolve) => setTimeout(resolve));

      // Named after the source conversation ("Proposal review" in the SessionService stub).
      expect(workflows.create).toHaveBeenCalledWith({ title: 'Proposal review', fromRun: { sessionId: 'src', runId: 'run1' } });
      expect(navigate).toHaveBeenCalledWith(['/workflows', 'SKL-1', 'proposal-review']);
    });

    it('leaves the name to the server while the conversation has no title yet', async () => {
      features.workflows = true;
      sessionTitle = 'New Conversation';
      workflows.create.mockResolvedValue({ workflow: { skillId: 'SKL-1', name: 'untitled-workflow' } });
      vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
      const { el } = render(run([stepRow(0, 'done'), stepRow(1, 'done')]));

      [...el.querySelectorAll('button')].find((b) => text(b) === 'Save as workflow')!.click();
      await new Promise((resolve) => setTimeout(resolve));

      expect(workflows.create).toHaveBeenCalledWith({ title: undefined, fromRun: { sessionId: 'src', runId: 'run1' } });
    });

    it('is offered on a stopped run', () => {
      features.workflows = true;
      const { el } = render(run([stepRow(0, 'done'), stepRow(1, 'running')], { status: 'stopped' }));
      expect(buttons(el)).toContain('Save as workflow');
    });

    it('waits until every step has settled', () => {
      features.workflows = true;
      const { el } = render(run([stepRow(0, 'done'), stepRow(1, 'running')]));
      expect(buttons(el)).not.toContain('Save as workflow');
    });

    it('is not offered where the host turns it off (the designer’s test run, PR-D.4)', () => {
      features.workflows = true;
      const { el } = render(run([stepRow(0, 'done'), stepRow(1, 'done')]), { offerSaveAsWorkflow: false });
      expect(buttons(el)).toEqual(['Bring back results']);
    });

    it('is never offered while workflows are off', () => {
      const { el } = render(run([stepRow(0, 'done'), stepRow(1, 'done')]));
      expect(buttons(el)).not.toContain('Save as workflow');
    });

    it('shows the server’s reason when a checklist run’s field set can’t be copied', async () => {
      features.workflows = true;
      const refusal = { status: 409, error: { detail: "The field set 'compliance' is no longer in the workflow this run started from, so a saved copy couldn't run its checklist step" } };
      workflows.create.mockRejectedValue(refusal);
      const messageFor = vi.spyOn(workflows, 'messageFor').mockImplementation((err: unknown) => (err as typeof refusal).error.detail);
      const value = run([stepRow(0, 'done'), { ...stepRow(1, 'done'), kind: 'checklist', agentId: '', agentName: 'Haiku 5.5', targetSessionId: undefined }]);
      value.steps = value.steps.map((step, i) => (i === 1 ? { ...step, kind: 'checklist', fieldSets: ['compliance'], reads: ['files'] } : step));
      const { el, detect } = render(value);

      [...el.querySelectorAll('button')].find((b) => text(b) === 'Save as workflow')!.click();
      await new Promise((resolve) => setTimeout(resolve));
      detect();

      expect(workflows.create).toHaveBeenCalledWith({ title: 'Proposal review', fromRun: { sessionId: 'src', runId: 'run1' } });
      expect(text(el.querySelector('[role="alert"]'))).toContain("The field set 'compliance' is no longer in the workflow");
      messageFor.mockRestore();
    });

    it('says why when the save is refused', async () => {
      features.workflows = true;
      workflows.create.mockRejectedValue(new Error('nope'));
      const { el, detect } = render(run([stepRow(0, 'done'), stepRow(1, 'done')]));
      [...el.querySelectorAll('button')].find((b) => text(b) === 'Save as workflow')!.click();
      await new Promise((resolve) => setTimeout(resolve));
      detect();
      expect(text(el.querySelector('[role="alert"]'))).toBe('It couldn’t be saved as a workflow.');
    });
  });

  describe('a saved workflow’s graph and merge (PR-D.5b)', () => {
    const merging = { skillId: 'sk', name: 'review', title: 'Review', fingerprint: 'f', resultMode: 'merge' as const, resultInstruction: 'Write one review.' };

    it('View as graph opens the run on the graph, in run mode, as a disclosure', () => {
      features.workflows = true;
      const { el, detect } = render(run([stepRow(0, 'done'), stepRow(1, 'running')]));
      const toggle = [...el.querySelectorAll('button')].find((b) => text(b) === 'View as graph')!;
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      expect(el.querySelector('app-workflow-graph')).toBeNull();

      toggle.click();
      detect();
      const graph = el.querySelector('app-workflow-graph')!;
      expect(graph).not.toBeNull();
      expect(toggle.getAttribute('aria-expanded')).toBe('true');
      expect(toggle.getAttribute('aria-controls')).toBe(graph.id);
      expect(text(graph.querySelector('[data-node-id="s1"]'))).toContain('Generalist');
      expect(graph.querySelector('[data-node-id="s1"]')?.getAttribute('data-state')).toBe('done');
      expect(text(toggle)).toBe('Hide graph');
      expect(graph.querySelector('[role="region"]')?.getAttribute('aria-label')).toBe('Graph of this run');
    });

    it('offers no graph while workflows are off, nor in the designer’s test run', () => {
      expect(buttons(render(run([stepRow(0, 'done')])).el)).not.toContain('View as graph');
      features.workflows = true;
      expect(buttons(render(run([stepRow(0, 'done')]), { offerSaveAsWorkflow: false }).el)).not.toContain('View as graph');
    });

    it('shows the merge instruction, and none for a card result', () => {
      expect(text(render(run([stepRow(0, 'running')], { workflow: merging })).el.querySelector('[data-testid="run-merge-instruction"]'))).toBe(
        'Merge: Write one review.',
      );
      expect(render(run([stepRow(0, 'running')])).el.querySelector('[data-testid="run-merge-instruction"]')).toBeNull();
    });

    it('offers Merge now while the result waits and nothing holds it', () => {
      onComposer.set(true);
      mergeHold.set(null);
      const { el } = render(run([stepRow(0, 'done'), stepRow(1, 'done')], { workflow: merging, resultDeliveryId: 'd1' }));
      const button = [...el.querySelectorAll('button')].find((b) => text(b) === 'Merge now')!;
      button.click();
      expect(merge.startNow).toHaveBeenCalledWith(expect.objectContaining({ runId: 'run1' }), 'd1');
      expect(merge.holdFor).toHaveBeenCalledWith(expect.anything(), 'd1', { manual: true });
    });

    it('no Merge now off screen, or for a card result', () => {
      onComposer.set(true);
      expect(buttons(render(run([stepRow(0, 'done')], { workflow: merging })).el)).not.toContain('Merge now');
      mergeHold.set(null);
      expect(buttons(render(run([stepRow(0, 'done')])).el)).not.toContain('Merge now');
    });
  });

  it('draws a prompt step as a line with no link, named by its step and model, its answer behind a disclosure', async () => {
    const prompt = (i: number, name: string, status: HandoffStatus, extra: Partial<HandoffView> = {}): HandoffView => ({
      ...stepRow(i, status),
      kind: 'prompt',
      agentId: '',
      agentName: i === 3 ? 'Sonnet 5.5' : 'Haiku 5.5',
      targetSessionId: undefined,
      ...extra,
      handoffId: `h${i + 1}`,
      request: name,
    });
    const rows = [
      prompt(0, 'generalist', 'done', { outputText: '**Strong** aims.\n\n[cut off at the output limit]' }),
      prompt(1, 'colleague', 'running'),
      prompt(2, 'fiscal', 'stopped', { stoppedReason: 'too_large' }),
      prompt(3, 'summary', 'open'),
    ];
    const value = run(rows);
    value.steps = value.steps.map((step, i) => ({
      ...step,
      kind: 'prompt',
      stepName: ['generalist', 'colleague', 'fiscal_outsider', 'summary'][i],
      ...(i === 3 ? { reads: ['generalist', 'colleague'] } : { reads: ['request', 'files'] }),
    }));
    pending.add('h4');
    // Parse the Markdown for real, but skip the post-render plugins (Mermaid, KaTeX), which tests don't load.
    TestBed.inject(MarkdownService).render = () => Promise.resolve();
    const { el, detect } = render(value);

    expect(text(el.querySelector('h3'))).toBe('Proposal review · 4 steps, 1 of 4 finished');
    expect(el.querySelectorAll('a').length).toBe(0);
    const lines = [...el.querySelectorAll('[data-handoff-id]')].map((line) => text(line));
    expect(lines).toEqual([
      'Generalist · Prompt · Haiku 5.5 · Finished Answer',
      'Colleague · Prompt · Haiku 5.5 · Working',
      'Fiscal outsider · Prompt · Haiku 5.5 · Stopped · the documents are too large for Haiku 5.5',
      'Summary · Prompt · Sonnet 5.5 · Waiting for Colleague',
    ]);
    // A stopped prompt step's line wraps, so its reason is never cut off on a phone; the rest stay one line.
    const spans = [...el.querySelectorAll('[data-handoff-id] > span.flex-1')];
    expect(spans.map((span) => span.classList.contains('truncate'))).toEqual([true, true, false, true]);
    expect(spans[2].classList.contains('break-words')).toBe(true);

    const toggle = [...el.querySelectorAll('button')].find((b) => text(b) === 'Answer')!;
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.getAttribute('aria-label')).toBe('Show Generalist’s answer');
    toggle.click();
    detect();
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    const answer = el.querySelector(`#${toggle.getAttribute('aria-controls')}`)!;
    expect(answer.getAttribute('role')).toBe('region');
    // The answer is the model's Markdown, rendered as an assistant message's is; the cut-off
    // marker is shown as the step wrote it.
    await new Promise((resolve) => setTimeout(resolve));
    detect();
    expect(answer.tagName).not.toBe('PRE');
    expect(answer.querySelector('strong')?.textContent).toBe('Strong');
    expect(answer.textContent).not.toContain('**');
    expect(answer.textContent).toContain('[cut off at the output limit]');
  });
  it('draws a checklist step as "{Step} · Checklist · {model}", its sets as tables behind Results, a failed set in words', () => {
    features.workflows = true;
    const checklist = (i: number, status: HandoffStatus, extra: Partial<HandoffView> = {}): HandoffView => ({
      ...stepRow(i, status),
      kind: 'checklist',
      agentId: '',
      agentName: 'Haiku 5.5',
      targetSessionId: undefined,
      ...extra,
    });
    const rows = [
      checklist(0, 'done', { outputText: '### Budget values' }),
      checklist(1, 'stopped', { stoppedReason: 'error' }),
      checklist(2, 'done', { outputText: '### Compliance\n| Field | Value | State |' }),
    ];
    const value = run(rows);
    value.steps = value.steps.map((step, i) => ({
      ...step,
      kind: 'checklist',
      stepName: ['compliance_audit', 'budget_audit', 'mentoring'][i],
      reads: ['files'],
      fieldSets: i === 0 ? ['budget', 'compliance'] : ['compliance'],
      // The third's set rows couldn't be read: its line falls back to its answer's text.
      checklist:
        i === 0
          ? [
              {
                runId: 'run1',
                stepId: 's1',
                fieldSet: 'budget',
                setIndex: 0,
                fieldSetTitle: 'Budget values',
                status: 'done',
                fields: [
                  { name: 'cap', title: 'Budget cap', value: '$600,000', state: 'supported', method: 'numeric', quote: 'up to $600,000', locator: { kind: 'page', value: 2, precision: 'measured' } },
                  { name: 'trips', title: 'Conference trips', value: '2', state: 'quote_unsupported', method: 'number_word', quote: 'Three trips', locator: { kind: 'page', value: 3, precision: 'estimated' } },
                ],
              },
              { runId: 'run1', stepId: 's1', fieldSet: 'compliance', setIndex: 1, fieldSetTitle: 'Compliance', status: 'error', error: 'cut_off', fields: [] },
            ]
          : [],
    }));
    const { el, detect } = render(value);

    expect(text(el.querySelector('h3'))).toBe('Proposal review · 3 steps, 2 of 3 finished');
    expect(el.querySelectorAll('a').length).toBe(0);
    const lines = [...el.querySelectorAll('[data-handoff-id]')].map((line) => text(line));
    expect(lines).toEqual([
      'Compliance audit · Checklist · Haiku 5.5 · 2 field sets · Finished Results',
      'Budget audit · Checklist · Haiku 5.5 · 1 field set · Stopped · something went wrong',
      'Mentoring · Checklist · Haiku 5.5 · 1 field set · Finished Answer',
    ]);
    // Save as workflow is offered: the server copies the checklist step's sets into the new skill.
    expect([...el.querySelectorAll('button')].some((b) => text(b) === 'Save as workflow')).toBe(true);
    // A checklist line names its model and sets, so it wraps on a phone rather than cut them off.
    const spans = [...el.querySelectorAll('[data-handoff-id] > span.flex-1')];
    expect(spans.every((span) => span.classList.contains('break-words') && !span.classList.contains('truncate'))).toBe(true);

    const toggle = [...el.querySelectorAll('button')].find((b) => text(b) === 'Results')!;
    expect(toggle.getAttribute('aria-label')).toBe('Show Compliance audit’s results');
    toggle.click();
    detect();
    const region = el.querySelector(`#${toggle.getAttribute('aria-controls')}`)!;
    expect(region.getAttribute('role')).toBe('region');
    const tables = region.querySelectorAll('app-document-extraction');
    expect(tables.length).toBe(1);
    // The extraction table's own rendering: values, states, quotes and hedged locators.
    expect(text(tables[0])).toContain('Budget values · 2 fields');
    expect(text(tables[0])).toContain('$600,000');
    expect(text(tables[0])).toContain('found on page 2');
    expect(text(tables[0])).toContain('not in the quoted passage on about page 3');
    expect(text(tables[0])).toContain('up to $600,000');
    expect(text(tables[0])).toContain('1 unconfirmed');
    // A set that failed on its own says why, in plain words, beside its title.
    expect(text(region.querySelector('[data-testid="checklist-set-error"]'))).toBe(
      'Compliance · Not filled in. The model’s answer ran past its output limit before it filled in the fields.',
    );
  });
});
