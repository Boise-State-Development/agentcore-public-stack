import { LiveAnnouncer } from '@angular/cdk/a11y';
import { Location } from '@angular/common';
import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AgentMentionService, MentionableAgent } from '../agents/services/agent-mention.service';
import { ModelService } from '../session/services/model/model.service';
import { fitScale } from './components/workflow-graph.component';
import { WorkflowDesignerPage } from './workflow-designer.page';
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

  beforeEach(() => {
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
      ],
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

    it('starts the saved workflow through D.2’s start service, from both buttons', async () => {
      await open({ skillId: 'SKL-1', name: 'proposal-review' });
      el.querySelector<HTMLButtonElement>('#wf-tab-preview')!.click();
      await settle();

      button('Start test run').click();
      await settle();
      button('Try in a chat').click();
      await settle();

      expect(start.mock.calls).toEqual([[{ skillId: 'SKL-1', name: 'proposal-review' }], [{ skillId: 'SKL-1', name: 'proposal-review' }]]);
      expect(service.validate).not.toHaveBeenCalled();
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
});

describe('fitScale', () => {
  it('scales the graph to the canvas but never below 60% or above actual size', () => {
    expect(fitScale({ width: 2300, height: 500 }, { width: 900, height: 600 })).toBe(0.6);
    expect(fitScale({ width: 1000, height: 500 }, { width: 900, height: 600 })).toBe(0.9);
    expect(fitScale({ width: 600, height: 400 }, { width: 900, height: 600 })).toBe(1);
  });
});
