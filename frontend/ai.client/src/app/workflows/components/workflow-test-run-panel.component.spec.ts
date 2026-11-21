// The designer's Test run tab (workflow-designer spec §6.2, PR-D.4). The sheet, the run's row and the
// result card are the thread's own components, specced where they live; here they are stand-ins, and
// the spec pins which one the tab shows when, and how it finds a run it doesn't hold.
import { Component, input, signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DeliveryCardsComponent } from '../../session/components/handoff/delivery-cards.component';
import { WorkflowRunRowComponent } from '../../session/components/handoff/workflow-run-row.component';
import { WorkflowRunSheetComponent } from '../../session/components/handoff/workflow-run-sheet.component';
import { DeliveryStateService } from '../../session/services/handoff/delivery-state.service';
import { RunReview, WorkflowRunView } from '../../session/services/handoff/workflow-run.models';
import { WorkflowRunStateService } from '../../session/services/handoff/workflow-run-state.service';
import { SessionService } from '../../session/services/session/session.service';
import { TestRunRecord, WorkflowTestRunService } from '../workflow-test-run.service';
import { WorkflowTestRunPanelComponent } from './workflow-test-run-panel.component';

@Component({ selector: 'app-workflow-run-sheet', template: '<p data-testid="sheet">{{ sessionId() }}</p>' })
class StubSheet {
  readonly sessionId = input.required<string>();
}

@Component({ selector: 'app-workflow-run-row', template: '<p data-testid="row">{{ run().runId }}:{{ offerSaveAsWorkflow() }}</p>' })
class StubRow {
  readonly run = input.required<WorkflowRunView>();
  readonly offerSaveAsWorkflow = input(true);
}

@Component({ selector: 'app-delivery-cards', template: '<p data-testid="result">{{ sessionId() }}</p>' })
class StubCards {
  readonly sessionId = input.required<string>();
}

const REF = { skillId: 'SKL-1', name: 'proposal-review' };

function run(status: WorkflowRunView['status'] = 'approved'): WorkflowRunView {
  return { runId: 'RUN-1', sessionId: 'test-conv', status, steps: [] } as unknown as WorkflowRunView;
}

describe('WorkflowTestRunPanelComponent', () => {
  let fixture: ComponentFixture<WorkflowTestRunPanelComponent>;
  let el: HTMLElement;
  const record = signal<TestRunRecord | undefined>(undefined);
  const stateRun = signal<WorkflowRunView | undefined>(undefined);
  const review = signal<RunReview | undefined>(undefined);
  const cards = signal<unknown[]>([]);
  const settled = signal(false);
  const testRuns = {
    recordFor: () => record(),
    runFor: () => stateRun(),
    progress: () => ({ settled: settled() }),
    setRunId: vi.fn(),
    forget: vi.fn(() => record.set(undefined)),
    load: vi.fn(),
  };

  beforeEach(() => {
    record.set(undefined);
    stateRun.set(undefined);
    review.set(undefined);
    cards.set([]);
    settled.set(false);
    for (const fn of [testRuns.setRunId, testRuns.forget, testRuns.load]) fn.mockClear();
    TestBed.configureTestingModule({
      imports: [WorkflowTestRunPanelComponent],
      providers: [
        provideRouter([]),
        { provide: WorkflowTestRunService, useValue: testRuns },
        { provide: WorkflowRunStateService, useValue: { reviewFor: (id: string | undefined) => (id === 'test-conv' ? review() : undefined) } },
        { provide: DeliveryStateService, useValue: { cardsFor: () => cards() } },
        {
          provide: SessionService,
          useValue: { mergedSessionsResource: () => ({ sessions: [{ sessionId: 'test-conv', title: 'Test: Proposal review' }] }) },
        },
      ],
    });
    TestBed.overrideComponent(WorkflowTestRunPanelComponent, {
      remove: { imports: [WorkflowRunSheetComponent, WorkflowRunRowComponent, DeliveryCardsComponent] },
      add: { imports: [StubSheet, StubRow, StubCards] },
    });
  });

  afterEach(() => fixture?.destroy());

  async function render(): Promise<void> {
    fixture = TestBed.createComponent(WorkflowTestRunPanelComponent);
    fixture.componentRef.setInput('ref', REF);
    el = fixture.nativeElement as HTMLElement;
    await detect();
  }

  async function detect(): Promise<void> {
    for (let i = 0; i < 3; i++) {
      fixture.detectChanges();
      await fixture.whenStable();
      await new Promise((resolve) => setTimeout(resolve));
    }
  }

  function text(): string {
    return (el.textContent ?? '').replace(/\s+/g, ' ').trim();
  }

  it('hosts the starter’s sheet for the test conversation, under a note on quota and what can be opened', async () => {
    record.set({ sessionId: 'test-conv' });
    review.set({ phase: 'loading' } as RunReview);
    await render();

    expect(el.querySelector('[data-testid="sheet"]')!.textContent).toBe('test-conv');
    expect(text()).toContain('on your quota');
    expect(text()).toContain('can be opened afterwards, from here or from “Test: Proposal review”');
    expect(testRuns.load).not.toHaveBeenCalled();
  });

  it('remembers the run’s id once the sheet has created it', async () => {
    record.set({ sessionId: 'test-conv' });
    review.set({ phase: 'loading' } as RunReview);
    await render();

    review.set({ phase: 'ready', created: { run: { runId: 'RUN-1' } } } as RunReview);
    await detect();

    expect(testRuns.setRunId).toHaveBeenCalledWith(REF, 'RUN-1');
  });

  it('shows the run’s own row once started, without Save as workflow, and the result card when it lands', async () => {
    record.set({ sessionId: 'test-conv', runId: 'RUN-1' });
    stateRun.set(run());
    await render();

    expect(el.querySelector('[data-testid="row"]')!.textContent).toBe('RUN-1:false');
    expect(el.querySelector('[data-testid="result"]')).toBeNull();
    const link = el.querySelector<HTMLAnchorElement>('a')!;
    expect(link.textContent).toContain('Open the test conversation');
    expect(link.getAttribute('href')).toBe('/s/test-conv');
    // A run still going can't be closed here by accident.
    expect(text()).not.toContain('Close test run');

    cards.set([{ deliveryId: 'D-1' }]);
    settled.set(true);
    await detect();

    expect(el.querySelector('[data-testid="result"]')!.textContent).toBe('test-conv');
    expect(text()).toContain('Close test run');
  });

  it('loads a run it doesn’t hold by id', async () => {
    testRuns.load.mockImplementation(async () => {
      stateRun.set(run());
      return 'loaded';
    });
    record.set({ sessionId: 'test-conv', runId: 'RUN-1' });
    await render();

    expect(testRuns.load).toHaveBeenCalledWith({ sessionId: 'test-conv', runId: 'RUN-1' });
    expect(testRuns.load).toHaveBeenCalledTimes(1);
    expect(el.querySelector('[data-testid="row"]')).not.toBeNull();
  });

  it('forgets a test run that never started: cancelled on its sheet', async () => {
    testRuns.load.mockResolvedValue('gone');
    record.set({ sessionId: 'test-conv', runId: 'RUN-1' });
    review.set({ phase: 'ready' } as RunReview);
    await render();

    review.set(undefined);
    await detect();

    expect(testRuns.load).toHaveBeenCalledTimes(1);
    expect(testRuns.forget).toHaveBeenCalledWith(REF);
  });

  it('forgets one cancelled before the run was even created, with no request', async () => {
    record.set({ sessionId: 'test-conv' });
    await render();

    expect(testRuns.load).not.toHaveBeenCalled();
    expect(testRuns.forget).toHaveBeenCalledWith(REF);
  });

  it('says so when the run can’t be loaded, and tries again on request', async () => {
    testRuns.load.mockResolvedValue('failed');
    record.set({ sessionId: 'test-conv', runId: 'RUN-1' });
    await render();

    expect(el.querySelector('[role="alert"]')!.textContent).toContain('couldn’t be loaded');
    el.querySelector<HTMLButtonElement>('[role="alert"] button')!.click();
    await detect();
    expect(testRuns.load).toHaveBeenCalledTimes(2);
  });
});
