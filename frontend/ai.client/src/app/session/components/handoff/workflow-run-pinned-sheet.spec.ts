import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WorkflowDocument, WorkflowFilesRule } from '../../../services/workflows/workflow.models';
import { ModelService } from '../../services/model/model.service';
import { WorkflowRunStateService } from '../../services/handoff/workflow-run-state.service';
import { RunReview, RunReviewStep } from '../../services/handoff/workflow-run.models';
import { WorkflowRunService } from '../../services/handoff/workflow-run.service';
import { AgentMentionService, MentionableAgent } from '../../../agents/services/agent-mention.service';
import { FEATURES } from '../../../services/features';
import { WorkflowRunSheetComponent, pinLine } from './workflow-run-sheet.component';

/**
 * Version pinning on the run sheet (workflow-designer §23.2, PR-D.8c): a pinned step says which
 * version it runs, offers Use version P in place of Mark as reviewed, tells the Agent's owner they
 * run the pin too, and names a refused version in the sheet's words.
 */

function document(options: { isOwned?: boolean; files?: WorkflowFilesRule } = {}): WorkflowDocument {
  return {
    workflow: {
      skillId: 'skill-1',
      name: 'review',
      title: 'Proposal review',
      version: 3,
      files: options.files ?? 'any',
      request: 'Review this proposal',
      steps: [
        {
          name: 'generalist',
          kind: 'agent',
          agentId: 'a1',
          agentVersion: 5,
          reads: ['request', 'files'],
          effectiveRequest: 'Review this proposal',
          agent: { canRun: true, name: 'Generalist', hasIcon: false, publishedVersion: 6, changed: true, runsDraft: false },
        },
        {
          name: 'colleague',
          kind: 'agent',
          agentId: 'a2',
          agentVersion: 2,
          reads: ['request', 'files'],
          effectiveRequest: 'Review this proposal',
          agent: { canRun: true, name: 'Colleague', hasIcon: false, publishedVersion: 2, changed: false, runsDraft: false },
        },
      ],
      result: { mode: 'card', steps: ['generalist', 'colleague'] },
      fingerprint: 'fp',
      isOwned: options.isOwned ?? true,
    },
    sheet: { startable: true, blockers: [] },
  };
}

function step(index: number, name: string, agentName: string, extra: Partial<RunReviewStep['workflowStep']> = {}): RunReviewStep {
  return {
    stepId: `s${index}`,
    handoffId: `h${index}`,
    agentId: `a${index}`,
    agentName,
    ownRequest: null,
    removed: false,
    workflowStep: { name, readsSteps: [], readsFiles: true, unavailable: false, changed: false, runsDraft: false, ...extra },
  };
}

function review(overrides: Partial<RunReview> = {}, docOptions: { isOwned?: boolean; files?: WorkflowFilesRule } = {}): RunReview {
  const doc = document(docOptions);
  return {
    draft: {
      agents: [
        { agentId: 'a1', agentName: 'Generalist' },
        { agentId: 'a2', agentName: 'Colleague' },
      ],
      request: 'Review this proposal',
      workflow: { ref: { skillId: 'skill-1', name: 'review' }, document: doc },
    },
    workflow: { ref: { skillId: 'skill-1', name: 'review' }, document: doc },
    phase: 'ready',
    request: 'Review this proposal',
    steps: [step(1, 'generalist', 'Generalist', { changed: true, publishedVersion: 6 }), step(2, 'colleague', 'Colleague')],
    card: '',
    cardRemoved: false,
    fileIds: [],
    ...overrides,
  };
}

class ModelServiceStub {
  readonly availableModels = signal([]);
  readonly selectedModel = signal({ modelId: 'm', modelName: 'Haiku 5.5', inputPricePerMillionTokens: 1 });
}

describe('WorkflowRunSheetComponent, a pinned step', () => {
  let fixture: ComponentFixture<WorkflowRunSheetComponent>;
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
    markReviewed: vi.fn(),
    useVersion: vi.fn(),
    refreshFiles: vi.fn(),
  };
  const load = vi.fn().mockResolvedValue(undefined);

  async function render(r: RunReview, own: string[] = []): Promise<void> {
    const mentionable = signal<MentionableAgent[]>(own.map((agentId) => ({ agentId, name: agentId, group: 'own' })));
    await TestBed.configureTestingModule({
      imports: [WorkflowRunSheetComponent],
      providers: [
        { provide: WorkflowRunService, useValue: runs },
        { provide: ModelService, useClass: ModelServiceStub },
        { provide: AgentMentionService, useValue: { mentionable, load } },
        { provide: FEATURES, useValue: { agentVersionPinning: true } },
      ],
    }).compileComponents();
    TestBed.inject(WorkflowRunStateService).setReview('src', r);
    fixture = TestBed.createComponent(WorkflowRunSheetComponent);
    fixture.componentRef.setInput('sessionId', 'src');
    fixture.detectChanges();
  }

  beforeEach(() => {
    Object.values(runs).forEach((fn) => fn.mockClear());
    load.mockClear();
  });
  afterEach(() => TestBed.resetTestingModule());

  const el = (): HTMLElement => fixture.nativeElement as HTMLElement;
  const buttons = (): HTMLButtonElement[] => [...el().querySelectorAll('button')];

  it('says which version a superseded step runs and offers Use version in place of Mark as reviewed', async () => {
    await render(
      review({
        steps: [
          step(1, 'generalist', 'Generalist', { changed: true, publishedVersion: 5, pin: { version: 3, canRun: true, offerVersion: 5 } }),
          step(2, 'colleague', 'Colleague', { pin: { version: 2, canRun: true } }),
        ],
      }),
    );
    const text = el().textContent ?? '';
    expect(text).toContain('Uses version 3. Version 5 is the current published version.');
    expect(text).toContain('Uses version 2.');
    expect(text).not.toContain('This Agent has changed since it was added');
    expect(buttons().some((b) => b.textContent?.includes('Mark as reviewed'))).toBe(false);
    const use = buttons().find((b) => b.textContent?.trim() === 'Use version 5')!;
    expect(use.getAttribute('aria-label')).toBe('Use version 5 of Generalist');
    use.click();
    expect(runs.useVersion).toHaveBeenCalledWith('src', 'generalist');
  });

  it('offers Use version to someone who can’t edit the workflow too, since it applies to their run', async () => {
    await render(
      review({ steps: [step(1, 'generalist', 'Generalist', { pin: { version: 3, canRun: true, offerVersion: 5 } }), step(2, 'colleague', 'Colleague')] }, { isOwned: false }),
    );
    expect(buttons().some((b) => b.textContent?.trim() === 'Use version 5')).toBe(true);
  });

  it('tells the Agent’s owner the pin runs for them too, with no draft line', async () => {
    await render(
      review({ steps: [step(1, 'generalist', 'Generalist', { pin: { version: 3, canRun: true } }), step(2, 'colleague', 'Colleague')] }),
      ['a1'],
    );
    const text = el().textContent ?? '';
    expect(text).toContain('Runs version 3 for everyone, including you.');
    expect(text).not.toContain('uses its draft');
    expect(load).toHaveBeenCalled();
  });

  it('a refused version keeps the Agent’s name, says why in the sheet’s words, and offers the current one', async () => {
    const why = 'Version 5 of this Agent is no longer published; version 3 is the current one. Use version 3, or remove the step.';
    await render(
      review({
        phase: 'blocked',
        steps: [
          step(1, 'generalist', 'Generalist', {
            unavailable: true,
            unavailableReason: why,
            pin: { version: 5, canRun: false, reason: 'above_published', offerVersion: 3 },
          }),
          step(2, 'colleague', 'Colleague'),
        ],
      }),
    );
    const text = el().textContent ?? '';
    expect(text).toContain('Generalist');
    expect(text).not.toContain('Unavailable Agent');
    expect(text).toContain(why);
    expect(text).not.toContain('Uses version 5');
    expect(buttons().some((b) => b.textContent?.trim() === 'Use version 3')).toBe(true);
  });

  it('a version chosen for this run only says so, with nothing more to offer', async () => {
    await render(
      review({ steps: [step(1, 'generalist', 'Generalist', { pin: { version: 5, canRun: true }, pinForThisRun: true }), step(2, 'colleague', 'Colleague')] }),
    );
    expect(el().textContent).toContain('Uses version 5 for this run.');
    expect(buttons().some((b) => b.textContent?.includes('Use version'))).toBe(false);
  });
});

describe('pinLine', () => {
  it('words a pin for the owner, for anyone else, and for this run only', () => {
    expect(pinLine({ pin: { version: 3, canRun: true } }, false)).toBe('Uses version 3.');
    expect(pinLine({ pin: { version: 3, canRun: true, offerVersion: 5 } }, true)).toBe(
      'Runs version 3 for everyone, including you. Version 5 is the current published version.',
    );
    expect(pinLine({ pin: { version: 5, canRun: true }, pinForThisRun: true }, true)).toBe('Uses version 5 for this run.');
    expect(pinLine({}, false)).toBeUndefined();
  });
});
