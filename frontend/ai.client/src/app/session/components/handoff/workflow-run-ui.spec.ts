import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ModelService } from '../../services/model/model.service';
import { HandoffStateService } from '../../services/handoff/handoff-state.service';
import { WorkflowRunQueueService } from '../../services/handoff/workflow-run-queue.service';
import { WorkflowRunStateService } from '../../services/handoff/workflow-run-state.service';
import { RunReview, WorkflowRunView } from '../../services/handoff/workflow-run.models';
import { WorkflowRunService } from '../../services/handoff/workflow-run.service';
import { WorkflowRunResumeComponent } from './workflow-run-resume.component';
import { WorkflowRunSheetComponent } from './workflow-run-sheet.component';

const NAMES = ['Generalist', 'Colleague', 'Emotional outsider', 'Fiscal outsider'];

function review(overrides: Partial<RunReview> = {}): RunReview {
  return {
    draft: { agents: NAMES.map((n, i) => ({ agentId: `a${i + 1}`, agentName: n })), request: 'review this' },
    phase: 'ready',
    request: 'review this',
    steps: NAMES.map((n, i) => ({
      stepId: `s${i + 1}`,
      handoffId: `h${i + 1}`,
      agentId: `a${i + 1}`,
      agentName: n,
      ownRequest: null,
      removed: false,
      ...(i < 2 ? { modelId: 'us.anthropic.claude-sonnet-5-5', inputPricePerMillionTokens: 3 } : {}),
    })),
    card: 'x'.repeat(4000),
    cardRemoved: false,
    files: [
      { uploadId: 'f1', filename: 'proposal.pdf', mimeType: 'application/pdf', sizeBytes: 10, estimatedTokens: 99_000, pages: 41 },
    ],
    fileIds: ['f1'],
    ...overrides,
  };
}

class ModelServiceStub {
  readonly availableModels = signal([{ modelId: 'us.anthropic.claude-sonnet-5-5', modelName: 'Sonnet 5.5' }]);
  readonly selectedModel = signal({ modelId: 'us.anthropic.claude-haiku-5-5', modelName: 'Haiku 5.5', inputPricePerMillionTokens: 1 });
}

describe('WorkflowRunSheetComponent', () => {
  let fixture: ComponentFixture<WorkflowRunSheetComponent>;
  let state: WorkflowRunStateService;
  const runs = {
    setRequest: vi.fn(),
    setStepRequest: vi.fn(),
    removeStep: vi.fn(),
    setCard: vi.fn(),
    setCardRemoved: vi.fn(),
    setFileShared: vi.fn(),
    useExcerpt: vi.fn(),
    previewPrompt: vi.fn().mockReturnValue('PROMPT'),
    cancel: vi.fn(),
    start: vi.fn(),
  };

  beforeEach(async () => {
    Object.values(runs).forEach((fn) => fn.mockClear());
    await TestBed.configureTestingModule({
      imports: [WorkflowRunSheetComponent],
      providers: [
        { provide: WorkflowRunService, useValue: runs },
        { provide: ModelService, useClass: ModelServiceStub },
      ],
    }).compileComponents();
    state = TestBed.inject(WorkflowRunStateService);
    state.setReview('src', review());
    fixture = TestBed.createComponent(WorkflowRunSheetComponent);
    fixture.componentRef.setInput('sessionId', 'src');
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  const el = (): HTMLElement => fixture.nativeElement as HTMLElement;
  const buttons = (): HTMLButtonElement[] => [...el().querySelectorAll('button')];

  it('is a region named for the Agents, with one primary Start', () => {
    const region = el().querySelector('[role="region"]');
    expect(region?.getAttribute('aria-label')).toBe('Hand off to 4 Agents');
    expect(el().querySelector('h2')?.textContent?.trim()).toBe('Hand off to 4 Agents');
    expect(buttons().filter((b) => b.className.includes('bg-primary-accessible')).map((b) => b.textContent?.trim())).toEqual([
      'Start 4 Agents',
    ]);
  });

  it('labels each Remove with its Agent and shows each step’s model', () => {
    const removes = buttons().filter((b) => b.getAttribute('aria-label')?.startsWith('Remove '));
    expect(removes.map((b) => b.getAttribute('aria-label'))).toEqual(NAMES.map((n) => `Remove ${n}`));
    expect(el().textContent).toContain('Sonnet 5.5');
    // A step whose Agent names no model runs on the send's.
    expect(el().textContent).toContain('Haiku 5.5');
  });

  it('moves focus to the next step’s Remove after a removal', async () => {
    runs.removeStep.mockImplementation((_src: string, stepId: string) => {
      const r = state.reviewFor('src')!;
      state.updateReview('src', { steps: r.steps.map((s) => (s.stepId === stepId ? { ...s, removed: true } : s)) });
    });
    buttons().find((b) => b.getAttribute('aria-label') === 'Remove Colleague')!.click();
    fixture.detectChanges();
    await fixture.whenStable();
    expect((document.activeElement as HTMLElement | null)?.getAttribute('aria-label')).toBe('Remove Emotional outsider');
    expect(el().querySelector('h2')?.textContent?.trim()).toBe('Hand off to 3 Agents');
  });

  it('has no Remove on the last step left', () => {
    state.setReview('src', review({ steps: review().steps.map((s, i) => ({ ...s, removed: i > 0 })) }));
    fixture.detectChanges();
    expect(buttons().some((b) => b.getAttribute('aria-label')?.startsWith('Remove '))).toBe(false);
    expect(el().textContent).toContain('Start 1 Agent');
  });

  it('shows the estimate, and says when it is unavailable', () => {
    // Two Sonnet 5.5 steps at $3/M and two Haiku 5.5 steps at $1/M, 100k tokens each at 1.40×.
    expect(el().textContent).toContain('About $1.12 to start, before any follow-up (estimate)');
    state.updateReview('src', { files: [{ ...review().files![0], estimatedTokens: null }] });
    fixture.detectChanges();
    expect(el().textContent).toContain('Cost estimate unavailable');
  });

  it('lists the files with their pages', () => {
    expect(el().textContent).toContain('proposal.pdf');
    expect(el().textContent).toContain('41 pages');
  });

  it('Esc cancels, and Start is disabled while the brief writes', () => {
    el().dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(runs.cancel).toHaveBeenCalledWith('src');
    state.updateReview('src', { brief: { status: 'writing', text: '' } });
    fixture.detectChanges();
    const start = buttons().find((b) => b.textContent?.includes('Start 4 Agents'))!;
    expect(start.disabled).toBe(true);
  });

  it('Preview asks which step', () => {
    buttons().find((b) => b.textContent?.includes('Context from this conversation'))!.click();
    fixture.detectChanges();
    buttons().find((b) => b.textContent?.trim() === 'Preview full prompt')!.click();
    fixture.detectChanges();
    const picker = el().querySelector('select') as HTMLSelectElement;
    expect([...picker.options].map((o) => o.textContent?.trim())).toEqual(NAMES);
    expect(runs.previewPrompt).toHaveBeenCalledWith(expect.anything(), 's1');
  });
});

describe('WorkflowRunResumeComponent', () => {
  afterEach(() => {
    TestBed.resetTestingModule();
    localStorage.clear();
  });

  function approvedRun(): WorkflowRunView {
    return {
      runId: 'run1',
      sessionId: 'src',
      status: 'approved',
      request: 'r',
      steps: NAMES.slice(0, 2).map((n, i) => ({
        stepId: `s${i + 1}`,
        handoffId: `h${i + 1}`,
        agentId: `a${i + 1}`,
        agentName: n,
        request: 'r',
        removed: false,
        handoff: {
          handoffId: `h${i + 1}`,
          sessionId: 'src',
          kind: 'mention',
          status: 'open',
          agentId: `a${i + 1}`,
          agentName: n,
          request: 'r',
          sourceMessageIndex: 1,
          excerptTokens: 1,
          createdAt: '',
          updatedAt: '',
        },
      })),
      fileIds: [],
      limits: { maxSteps: 8, concurrency: 4 },
      sourceMessageIndex: 1,
      createdAt: '',
      updatedAt: '',
    };
  }

  async function render(saved: boolean): Promise<{ fixture: ComponentFixture<WorkflowRunResumeComponent>; resume: ReturnType<typeof vi.fn> }> {
    const resume = vi.fn();
    const queue = {
      unstartedSteps: (run: WorkflowRunView) =>
        run.steps.map((s) => ({ stepId: s.stepId, handoffId: s.handoffId, agentId: s.agentId, agentName: s.agentName, request: s.request })),
      storedEnvelope: () => (saved ? ({ runId: 'run1' } as never) : null),
    };
    await TestBed.configureTestingModule({
      imports: [WorkflowRunResumeComponent],
      providers: [
        { provide: WorkflowRunService, useValue: { resume } },
        { provide: WorkflowRunQueueService, useValue: queue },
        HandoffStateService,
      ],
    }).compileComponents();
    TestBed.inject(WorkflowRunStateService).hydrate('src', [approvedRun()]);
    const fixture = TestBed.createComponent(WorkflowRunResumeComponent);
    fixture.componentRef.setInput('sessionId', 'src');
    fixture.detectChanges();
    return { fixture, resume };
  }

  it('shows the unstarted steps as Not started, with one Start', async () => {
    const { fixture, resume } = await render(true);
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Generalist · Not started');
    expect(text).toContain('Colleague · Not started');
    const start = [...(fixture.nativeElement as HTMLElement).querySelectorAll('button')];
    expect(start.map((b) => b.textContent?.trim())).toEqual(['Start 2 Agents']);
    start[0].click();
    expect(resume).toHaveBeenCalled();
  });

  it('offers no Start without the envelope this browser saved', async () => {
    const { fixture } = await render(false);
    expect((fixture.nativeElement as HTMLElement).querySelectorAll('button').length).toBe(0);
    expect((fixture.nativeElement as HTMLElement).textContent).toContain('approved in another browser');
  });
});
