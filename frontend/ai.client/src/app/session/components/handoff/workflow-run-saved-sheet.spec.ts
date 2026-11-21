import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WorkflowDocument, WorkflowFilesRule } from '../../../services/workflows/workflow.models';
import { ModelService } from '../../services/model/model.service';
import { WorkflowRunStateService } from '../../services/handoff/workflow-run-state.service';
import { RunReview, RunReviewStep } from '../../services/handoff/workflow-run.models';
import { WorkflowRunService } from '../../services/handoff/workflow-run.service';
import { WorkflowRunSheetComponent, changedLine } from './workflow-run-sheet.component';

/**
 * The run sheet for a saved workflow (workflow-designer.md §8.3, §10, §11, PR-D.2): titled with
 * the workflow, a step the starter can't run blocks Start until removed, and a step whose Agent
 * has moved on says so, with Mark as reviewed for the workflow's owner only.
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
    workflowStep: { name, readsSteps: [], unavailable: false, changed: false, runsDraft: false, ...extra },
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

describe('WorkflowRunSheetComponent, for a saved workflow', () => {
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
    markReviewed: vi.fn(),
    refreshFiles: vi.fn(),
  };

  async function render(r: RunReview): Promise<void> {
    await TestBed.configureTestingModule({
      imports: [WorkflowRunSheetComponent],
      providers: [
        { provide: WorkflowRunService, useValue: runs },
        { provide: ModelService, useClass: ModelServiceStub },
      ],
    }).compileComponents();
    state = TestBed.inject(WorkflowRunStateService);
    state.setReview('src', r);
    fixture = TestBed.createComponent(WorkflowRunSheetComponent);
    fixture.componentRef.setInput('sessionId', 'src');
    fixture.detectChanges();
  }

  beforeEach(() => Object.values(runs).forEach((fn) => fn.mockClear()));
  afterEach(() => TestBed.resetTestingModule());

  const el = (): HTMLElement => fixture.nativeElement as HTMLElement;
  const buttons = (): HTMLButtonElement[] => [...el().querySelectorAll('button')];
  const start = (): HTMLButtonElement => buttons().find((b) => b.textContent?.includes('Start '))!;

  it('scrolls its body between a fixed header and footer, so Start stays on screen', async () => {
    await render(review());
    const body = el().querySelector('[data-testid="run-sheet-body"]')!;
    const footer = el().querySelector('[data-testid="run-sheet-footer"]')!;
    expect(body.className).toContain('overflow-y-auto');
    expect(body.contains(el().querySelector('h2'))).toBe(false);
    expect(footer.contains(start())).toBe(true);
    expect(body.contains(start())).toBe(false);
    expect(body.querySelector('textarea')).not.toBeNull();
  });

  it('is titled with the workflow’s name', async () => {
    await render(review());
    expect(el().querySelector('h2')?.textContent?.trim()).toBe('Proposal review');
    expect(el().querySelector('[role="region"]')?.getAttribute('aria-label')).toBe('Proposal review');
  });

  it('says a step’s Agent has changed, following §10', async () => {
    await render(review());
    expect(el().textContent).toContain(
      'This Agent has changed since it was added. A run uses its published version, now version 6.',
    );
    expect(el().textContent).not.toContain('See what changed');
    expect(changedLine(4, true)).toBe(
      'This Agent has changed since it was added. A run uses its published version, now version 4. You own it, so your run uses its draft.',
    );
  });

  it('offers Mark as reviewed to the workflow’s owner', async () => {
    await render(review());
    const mark = buttons().find((b) => b.getAttribute('aria-label') === 'Mark Generalist as reviewed');
    expect(mark).toBeDefined();
    // Only on the step that changed.
    expect(buttons().filter((b) => b.textContent?.trim() === 'Mark as reviewed')).toHaveLength(1);
    mark!.click();
    expect(runs.markReviewed).toHaveBeenCalledWith('src', 'generalist');
  });

  it('never offers Mark as reviewed to someone the workflow is shared with', async () => {
    await render(review({}, { isOwned: false }));
    expect(el().textContent).toContain('This Agent has changed since it was added.');
    expect(buttons().some((b) => b.textContent?.includes('Mark as reviewed'))).toBe(false);
  });

  it('shows a step the starter can’t run, with why, and Start stays off until it is removed', async () => {
    await render(
      review({
        phase: 'blocked',
        steps: [
          step(1, 'generalist', 'Generalist'),
          { ...step(2, 'colleague', ''), handoffId: '', workflowStep: { name: 'colleague', readsSteps: [], unavailable: true, changed: false, runsDraft: false } },
        ],
      }),
    );
    expect(el().textContent).toContain('Unavailable Agent (colleague)');
    expect(el().textContent).toContain("You can't use this step's Agent. Remove the step, or ask its owner to share it.");
    expect(el().textContent).toContain("Remove the steps you can't use to continue.");
    expect(start().disabled).toBe(true);
    // No Edit on a step that can't run; Remove is the way forward.
    expect(buttons().some((b) => b.getAttribute('aria-label') === 'Edit the request for Unavailable Agent (colleague)')).toBe(false);
    buttons().find((b) => b.getAttribute('aria-label') === 'Remove Unavailable Agent (colleague)')!.click();
    expect(runs.removeStep).toHaveBeenCalledWith('src', 's2');
  });

  it('a workflow that takes one file keeps Start off until exactly one is ticked', async () => {
    const files = [
      { uploadId: 'f1', filename: 'proposal.pdf', mimeType: 'application/pdf', sizeBytes: 10, estimatedTokens: 100, pages: 2 },
    ];
    await render(review({ files, fileIds: [] }, { files: 'one' }));
    expect(el().textContent).toContain('This workflow takes exactly one file.');
    expect(start().disabled).toBe(true);
    state.updateReview('src', { fileIds: ['f1'] });
    fixture.detectChanges();
    expect(start().disabled).toBe(false);
  });

  it('a workflow that takes one file, in a conversation with none, says how to add it', async () => {
    await render(review({ files: [] }, { files: 'one' }));
    buttons().find((b) => b.textContent?.trim() === 'look again')!.click();
    expect(runs.refreshFiles).toHaveBeenCalledWith('src');
  });

  it('a workflow that takes no files lists none', async () => {
    const files = [{ uploadId: 'f1', filename: 'notes.txt', mimeType: 'text/plain', sizeBytes: 10 }];
    await render(review({ files }, { files: 'none' }));
    expect(el().textContent).not.toContain('notes.txt');
    expect(start().disabled).toBe(false);
  });

  describe('removing a step that others read (PR-D.5a)', () => {
    const staged = (): RunReview =>
      review({
        steps: [
          step(1, 'generalist', 'Generalist'),
          step(2, 'colleague', 'Colleague'),
          step(3, 'summary', 'Summary', { readsSteps: ['generalist'] }),
          step(4, 'editor', 'Editor', { readsSteps: ['summary'] }),
        ],
      });
    const remove = (label: string): HTMLButtonElement => buttons().find((b) => b.getAttribute('aria-label') === label)!;

    it('names the steps that go with it, on Remove and after', async () => {
      await render(staged());

      expect(remove('Remove Colleague')).toBeDefined();
      expect(remove('Remove Summary and the step that reads it')).toBeDefined();
      remove('Remove Generalist and the 2 steps that read it').click();
      fixture.detectChanges();

      expect(runs.removeStep).toHaveBeenCalledWith('src', 's1');
      expect(el().textContent).toContain('Removed Generalist, and Summary and Editor, which read it.');
    });

    it('a step every other step reads can’t be removed, and says so', async () => {
      await render(review({ steps: [step(1, 'generalist', 'Generalist'), step(2, 'summary', 'Summary', { readsSteps: ['generalist'] })] }));

      remove('Remove Generalist and the step that reads it').click();
      fixture.detectChanges();

      expect(runs.removeStep).not.toHaveBeenCalled();
      expect(el().textContent).toContain('Generalist can’t be removed: every other step reads it.');
    });
  });

  describe('the small graph and the merge (PR-D.5b)', () => {
    function merging(): RunReview {
      const r = review();
      const doc = {
        ...r.workflow!.document,
        workflow: { ...r.workflow!.document.workflow, result: { mode: 'merge', steps: ['generalist', 'colleague'], instruction: 'Write one review.' } },
        sheet: { startable: true, blockers: [], mergeInstruction: 'Write one review.' },
      };
      return { ...r, workflow: { ...r.workflow!, document: doc } };
    }

    it('says before Start that the merge starts by itself, and when it waits', async () => {
      await render(merging());
      const notice = el().querySelector('[data-testid="run-merge-notice"]');
      expect(notice?.textContent?.replace(/\s+/g, ' ')).toContain('Then this conversation merges the results: Write one review.');
      expect(notice?.textContent?.replace(/\s+/g, ' ')).toContain(
        'The merge starts by itself when every Agent finishes, if this conversation is open and nothing is typed in its composer. Otherwise the results wait on your composer.',
      );
    });

    it('a card result says nothing about a merge', async () => {
      await render(review());
      expect(el().querySelector('[data-testid="run-merge-notice"]')).toBeNull();
    });

    it('Show as a graph draws the steps it would start, as a disclosure', async () => {
      await render(merging());
      const toggle = buttons().find((b) => b.textContent?.includes('Show as a graph'))!;
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      toggle.click();
      fixture.detectChanges();

      const graph = el().querySelector('app-workflow-graph')!;
      expect(graph).not.toBeNull();
      expect(toggle.getAttribute('aria-controls')).toBe(graph.id);
      expect(graph.querySelector('[data-node-id="s1"]')?.textContent).toContain('Starts first');
      expect(graph.querySelector('[data-node-id="result"]')?.textContent).toContain('Merged in this conversation');
      expect(graph.querySelector('[role="region"]')?.getAttribute('aria-label')).toBe('Graph of the steps to start');
    });
  });
});
