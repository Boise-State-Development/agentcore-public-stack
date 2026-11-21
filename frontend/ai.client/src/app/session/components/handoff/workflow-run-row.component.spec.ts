// A workflow run's row in its source thread (workflow-runs spec §20.2, PR-W.4b). The services are
// stubbed; the run's progress rules are pinned in workflow-run-progress.spec.ts and the actions'
// HTTP in workflow-run.service.spec.ts / workflow-run-result.service.spec.ts.
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
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
  const onComposer = signal(false);
  const runs = { resume: vi.fn(), stop: vi.fn() };
  const results = { collect: vi.fn(), isCollecting: () => false, isOnComposer: () => onComposer() };
  const features = { workflows: false };
  let sessionTitle = 'Proposal review';
  const workflows = { create: vi.fn(), messageFor: (_err: unknown, fallback: string) => fallback };

  beforeEach(() => {
    pending.clear();
    loading.clear();
    onComposer.set(false);
    runs.resume.mockClear();
    runs.stop.mockClear();
    results.collect.mockClear();
    workflows.create.mockReset();
    features.workflows = false;
    sessionTitle = 'Proposal review';
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
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
        { provide: WorkflowRunQueueService, useValue: { isPending: (_run: string, id: string) => pending.has(id) } },
        { provide: WorkflowRunService, useValue: runs },
        { provide: WorkflowRunResultService, useValue: results },
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

      expect(buttons(el)).toEqual(['Bring back results', 'Save as workflow']);
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
});
