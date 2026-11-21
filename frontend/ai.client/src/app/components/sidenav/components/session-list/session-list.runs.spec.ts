import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { Dialog } from '@angular/cdk/dialog';
import { signal } from '@angular/core';
import { of } from 'rxjs';
import { SessionService } from '../../../../session/services/session/session.service';
import { SidenavService } from '../../../../services/sidenav/sidenav.service';
import { ToastService } from '../../../../services/toast/toast.service';
import { ProjectsService } from '../../../../projects/services/projects.service';
import { FEATURES } from '../../../../services/features';
import { AgentPinService } from '../../../../agents/services/agent-pin.service';
import { ChatStateService } from '../../../../session/services/chat/chat-state.service';
import { HandoffStateService } from '../../../../session/services/handoff/handoff-state.service';
import { WorkflowRunStateService } from '../../../../session/services/handoff/workflow-run-state.service';
import { WorkflowRunQueueService } from '../../../../session/services/handoff/workflow-run-queue.service';
import { HandoffStatus, HandoffView } from '../../../../session/services/handoff/handoff.models';
import { WorkflowRunView } from '../../../../session/services/handoff/workflow-run.models';
import { SessionMetadata } from '../../../../session/services/models/session-metadata.model';
import { SessionList } from './session-list';

/** Workflow runs' step conversations nested under their source (workflow-runs spec §20.4). */

const TODAY = new Date().toISOString();
const OLD = '2024-01-01T00:00:00Z';
const NAMES = ['Generalist', 'Colleague', 'Emotional outsider', 'Fiscal outsider'];

function session(id: string, extra: Partial<SessionMetadata> = {}): SessionMetadata {
  return {
    sessionId: id, userId: 'u', title: `Title ${id}`, status: 'active',
    createdAt: TODAY, lastMessageAt: TODAY, messageCount: 2, ...extra,
  };
}

function step(i: number, extra: Partial<SessionMetadata> = {}): SessionMetadata {
  return session(`t${i + 1}`, { spawnedFrom: { sessionId: 'src', handoffId: `h${i + 1}`, runId: 'run1' }, ...extra });
}

function row(i: number, status: HandoffStatus): HandoffView {
  return {
    handoffId: `h${i + 1}`, sessionId: 'src', kind: 'mention', status, agentId: `a${i + 1}`,
    agentName: NAMES[i], request: 'review', sourceMessageIndex: 1, excerptTokens: 1,
    createdAt: '', updatedAt: '', runId: 'run1', stepId: `s${i + 1}`,
    ...(status === 'open' ? {} : { targetSessionId: `t${i + 1}` }),
  };
}

function run(statuses: HandoffStatus[]): WorkflowRunView {
  return {
    runId: 'run1', sessionId: 'src', status: 'approved', request: 'review',
    steps: statuses.map((status, i) => {
      const handoff = row(i, status);
      return { stepId: `s${i + 1}`, handoffId: handoff.handoffId, agentId: handoff.agentId, agentName: NAMES[i], request: 'review', removed: false, handoff };
    }),
    fileIds: [], limits: { maxSteps: 8, concurrency: 4 }, sourceMessageIndex: 1, createdAt: '', updatedAt: '',
  };
}

describe('SessionList: a run\'s step conversations', () => {
  let sessions: ReturnType<typeof signal<{ sessions: SessionMetadata[]; nextToken: null }>>;
  let current: ReturnType<typeof signal<SessionMetadata>>;
  let features: Record<string, boolean>;

  beforeEach(() => {
    TestBed.resetTestingModule();
    sessions = signal({ sessions: [] as SessionMetadata[], nextToken: null });
    current = signal(session('elsewhere'));
    features = { projects: true, handoffs: true, workflowRuns: true };
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: AgentPinService, useValue: { pins: signal([]), load: vi.fn().mockResolvedValue([]), unpin: vi.fn() } },
        {
          provide: SessionService,
          useValue: {
            mergedSessionsResource: sessions,
            sessionsResource: { value: () => sessions(), error: () => null, isLoading: signal(false) },
            currentSession: current,
            isLoadingMoreSessions: signal(false),
            loadMoreSessionsError: signal(false),
            loadMoreSessions: vi.fn().mockResolvedValue(undefined),
            isLocallyRead: () => false,
            pinnedSessions: signal([]),
            pinnedSessionIds: signal(new Set<string>()),
          },
        },
        { provide: SidenavService, useValue: { close: vi.fn() } },
        { provide: ToastService, useValue: { success: vi.fn(), error: vi.fn() } },
        { provide: Dialog, useValue: { open: vi.fn().mockReturnValue({ closed: of(true) }) } },
        { provide: ProjectsService, useValue: { projects$: signal([]), available$: signal(null), loading$: signal(false), load: vi.fn() } },
        { provide: FEATURES, useFactory: () => features },
        { provide: WorkflowRunQueueService, useValue: { isPending: () => false } },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  function render() {
    const fixture = TestBed.createComponent(SessionList);
    fixture.detectChanges();
    return fixture;
  }

  const RUN_URL = /\/sessions\/src\/workflow-runs\/run1$/;
  const runReads = () => TestBed.inject(HttpTestingController).match((req) => RUN_URL.test(req.url));
  const oneRead = () => {
    const reads = runReads();
    expect(reads.length).toBe(1);
    return reads[0];
  };
  const text = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim();
  const line = (el: HTMLElement) => el.querySelector<HTMLButtonElement>('button[data-run-line]');
  const titles = (el: HTMLElement) => [...el.querySelectorAll('a[href^="/s/"]')].map((a) => text(a));

  it('nests the steps under their source as one collapsed line, counted as the run row counts', () => {
    TestBed.inject(WorkflowRunStateService).hydrate('src', [run(['done', 'done', 'running', 'open'])]);
    sessions.set({ sessions: [step(0), session('src'), step(1), step(2)], nextToken: null });
    const fixture = render();
    expect(runReads()).toEqual([]);

    const button = line(fixture.nativeElement)!;
    // 4 kept steps, a queued one with no conversation yet included, as the run row counts them.
    expect(text(button)).toBe('4 Agents · 2 of 4 finished');
    expect(button.getAttribute('aria-expanded')).toBe('false');
    expect(button.hasAttribute('aria-controls')).toBe(false);
    expect(titles(fixture.nativeElement)).toEqual(['Title src']);

    button.click();
    fixture.detectChanges();
    expect(button.getAttribute('aria-expanded')).toBe('true');
    const list = fixture.nativeElement.querySelector(`#${button.getAttribute('aria-controls')}`);
    // The ordinary row template, in the run's step order, each opening its own conversation.
    expect([...list.querySelectorAll('a')].map((a: Element) => a.getAttribute('href'))).toEqual(['/s/t1', '/s/t2', '/s/t3']);
    expect(titles(fixture.nativeElement)).toEqual(['Title src', 'Title t1', 'Title t2', 'Title t3']);
  });

  it('lists every step alone with the flag off', () => {
    features = { projects: true, handoffs: true, workflowRuns: false };
    sessions.set({ sessions: [session('src'), step(0), step(1)], nextToken: null });
    const fixture = render();
    expect(line(fixture.nativeElement)).toBeNull();
    expect(titles(fixture.nativeElement)).toEqual(['Title src', 'Title t1', 'Title t2']);
  });

  it('lists a step alone when its source is in another bucket or off the loaded page', () => {
    sessions.set({ sessions: [step(0), step(1, { spawnedFrom: { sessionId: 'gone', runId: 'run2' } }), session('src', { lastMessageAt: OLD, createdAt: OLD })], nextToken: null });
    const fixture = render();
    expect(line(fixture.nativeElement)).toBeNull();
    expect(titles(fixture.nativeElement)).toEqual(['Title t1', 'Title t2', 'Title src']);
  });

  it('shows the waiting mark on the collapsed line, ahead of a streaming step, and hands it back when open', () => {
    TestBed.inject(WorkflowRunStateService).hydrate('src', [run(['running', 'needs_input'])]);
    sessions.set({ sessions: [session('src'), step(0), step(1)], nextToken: null });
    TestBed.inject(ChatStateService).setChatLoading('t1', true);
    const fixture = render();
    const button = line(fixture.nativeElement)!;
    expect(text(button)).toBe('2 Agents · 0 of 2 finished (needs your input)');
    expect(button.querySelector('.bg-state-warning-700')).not.toBeNull();

    button.click();
    fixture.detectChanges();
    expect(text(button)).toBe('2 Agents · 0 of 2 finished');
  });

  it('shows the streaming dot when a step streams and none waits', () => {
    TestBed.inject(WorkflowRunStateService).hydrate('src', [run(['running', 'done'])]);
    sessions.set({ sessions: [session('src'), step(0), step(1)], nextToken: null });
    TestBed.inject(ChatStateService).setChatLoading('t1', true);
    const fixture = render();
    expect(text(line(fixture.nativeElement))).toBe('2 Agents · 1 of 2 finished (response in progress)');
  });

  it('leaves an unread step to its own row: the collapsed line shows no mark for it', () => {
    TestBed.inject(WorkflowRunStateService).hydrate('src', [run(['done', 'done'])]);
    sessions.set({ sessions: [session('src'), step(0, { unread: true }), step(1)], nextToken: null });
    const fixture = render();
    const button = line(fixture.nativeElement)!;
    expect(text(button)).toBe('2 Agents · 2 of 2 finished');
    expect(button.querySelector('.rounded-full')).toBeNull();
  });

  it('opens on its own while a step is the current conversation', () => {
    TestBed.inject(WorkflowRunStateService).hydrate('src', [run(['done', 'running'])]);
    sessions.set({ sessions: [session('src'), step(0), step(1)], nextToken: null });
    current.set(step(1));
    const fixture = render();
    expect(line(fixture.nativeElement)!.getAttribute('aria-expanded')).toBe('true');
  });

  it('nests a step started in this tab before the list knows its link', () => {
    TestBed.inject(HandoffStateService).hydrate('src', [row(0, 'running')], null);
    sessions.set({ sessions: [session('t1'), session('src')], nextToken: null });
    const fixture = render();
    oneRead();
    expect(text(line(fixture.nativeElement))).toBe('1 Agent');
  });

  /**
   * Run `r`'s `count` started step conversations under source `src{r}`, each settled (done) unless
   * `running` names it, of a run that kept `kept` steps (all of them started, by default).
   */
  function runOf(r: number, count: number, running: number[] = [], kept = count): SessionMetadata[] {
    const steps = Array.from({ length: count }, (_, i) => session(`r${r}t${i + 1}`, {
      spawnedFrom: {
        sessionId: `src${r}`, handoffId: `r${r}h${i + 1}`, runId: `run${r}`, runSteps: kept,
        ...(running.includes(i) ? {} : { settled: true }),
      },
    }));
    return [session(`src${r}`), ...steps];
  }
  const anyRunReads = () => TestBed.inject(HttpTestingController).match((req) => /\/workflow-runs\//.test(req.url));
  const lines = (el: HTMLElement) => [...el.querySelectorAll<HTMLButtonElement>('button[data-run-line]')].map((b) => text(b));

  it('reads nothing on a page load whose runs are all finished, and counts them from the list', () => {
    sessions.set({ sessions: [...runOf(1, 4), ...runOf(2, 3), ...runOf(3, 1), ...runOf(4, 2), ...runOf(5, 5)], nextToken: null });
    const fixture = render();
    expect(anyRunReads()).toEqual([]);
    expect(lines(fixture.nativeElement)).toEqual([
      '4 Agents · 4 of 4 finished',
      '3 Agents · 3 of 3 finished',
      '1 Agent · 1 of 1 finished',
      '2 Agents · 2 of 2 finished',
      '5 Agents · 5 of 5 finished',
    ]);

    // The list loading again, or a finished step getting a later message, still reads nothing.
    sessions.set({ sessions: [...runOf(1, 4), ...runOf(2, 3), ...runOf(3, 1), ...runOf(4, 2), ...runOf(5, 5)].map((s) => ({ ...s, lastMessageAt: new Date(Date.now() + 1000).toISOString() })), nextToken: null });
    fixture.detectChanges();
    expect(anyRunReads()).toEqual([]);
  });

  it('reads only the run still going on a page load with finished ones beside it', async () => {
    sessions.set({ sessions: [...runOf(1, 4), ...runOf(2, 3, [1]), ...runOf(3, 2)], nextToken: null });
    const fixture = render();
    const reads = anyRunReads();
    expect(reads.map((r) => r.request.url.replace(/.*\/sessions\//, ''))).toEqual(['src2/workflow-runs/run2']);
    expect(lines(fixture.nativeElement)[1]).toBe('3 Agents');

    // The running run keeps today's behaviour: counted from its read, read again when a step moves on.
    reads[0].flush({ ...run(['done', 'running', 'done']), runId: 'run2', sessionId: 'src2' });
    await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
    expect(lines(fixture.nativeElement)[1]).toBe('3 Agents · 2 of 3 finished');

    const moved = runOf(2, 3, [1]).map((s) => (s.sessionId === 'r2t2' ? { ...s, lastMessageAt: new Date(Date.now() + 1000).toISOString() } : s));
    sessions.set({ sessions: [...runOf(1, 4), ...moved, ...runOf(3, 2)], nextToken: null });
    fixture.detectChanges();
    const again = anyRunReads();
    expect(again.map((r) => r.request.url.replace(/.*\/sessions\//, ''))).toEqual(['src2/workflow-runs/run2']);
    again[0].flush({ ...run(['done', 'done', 'done']), runId: 'run2', sessionId: 'src2' });
    await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
    expect(lines(fixture.nativeElement)[1]).toBe('3 Agents · 3 of 3 finished');
  });

  it('reads a run between stages on load: every started step done, a later one still to start', async () => {
    // Stage 1's three steps are done; the summarizer that reads them is queued, with no conversation yet.
    sessions.set({ sessions: [...runOf(1, 3, [], 4)], nextToken: null });
    const fixture = render();
    const reads = anyRunReads();
    expect(reads.map((r) => r.request.url.replace(/.*\/sessions\//, ''))).toEqual(['src1/workflow-runs/run1']);
    expect(lines(fixture.nativeElement)).toEqual(['3 Agents']);
    reads[0].flush({ ...run(['done', 'done', 'done', 'open']), runId: 'run1', sessionId: 'src1' });
    await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
    expect(lines(fixture.nativeElement)).toEqual(['4 Agents · 3 of 4 finished']);
  });

  it('reads a run whose rows carry no kept count, as before', () => {
    const rows = runOf(1, 2).map((s) => (s.spawnedFrom ? { ...s, spawnedFrom: { ...s.spawnedFrom, runSteps: undefined } } : s));
    sessions.set({ sessions: rows, nextToken: null });
    const fixture = render();
    expect(anyRunReads().length).toBe(1);
    expect(lines(fixture.nativeElement)).toEqual(['2 Agents']);
  });

  it('reads a finished run once, when its line is opened, for the step order', async () => {
    sessions.set({ sessions: [session('src'), step(1, { spawnedFrom: { sessionId: 'src', handoffId: 'h2', runId: 'run1', runSteps: 2, settled: true } }), step(0, { spawnedFrom: { sessionId: 'src', handoffId: 'h1', runId: 'run1', runSteps: 2, settled: true } })], nextToken: null });
    const fixture = render();
    expect(runReads()).toEqual([]);
    const button = line(fixture.nativeElement)!;
    expect(text(button)).toBe('2 Agents · 2 of 2 finished');

    button.click();
    fixture.detectChanges();
    oneRead().flush(run(['done', 'done']));
    await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
    expect(titles(fixture.nativeElement)).toEqual(['Title src', 'Title t1', 'Title t2']);

    // Closed and opened again: nothing it holds can change, so no second read.
    button.click();
    fixture.detectChanges();
    button.click();
    fixture.detectChanges();
    expect(runReads()).toEqual([]);
  });

  it('reads a run this tab does not hold once per change in its steps, and counts from it', async () => {
    sessions.set({ sessions: [session('src'), step(0), step(1)], nextToken: null });
    const fixture = render();
    expect(text(line(fixture.nativeElement))).toBe('2 Agents');
    oneRead().flush(run(['done', 'running', 'open']));
    await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
    expect(text(line(fixture.nativeElement))).toBe('3 Agents · 1 of 3 finished');

    // The same list again: no second read.
    sessions.set({ sessions: [session('src'), step(0), step(1)], nextToken: null });
    fixture.detectChanges();
    expect(runReads()).toEqual([]);

    // A step moved on in the list: read again.
    sessions.set({ sessions: [session('src'), step(0), step(1, { lastMessageAt: new Date(Date.now() + 1000).toISOString() })], nextToken: null });
    fixture.detectChanges();
    oneRead().flush(run(['done', 'done', 'open']));
    await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
    expect(text(line(fixture.nativeElement))).toBe('3 Agents · 2 of 3 finished');
    expect(runReads()).toEqual([]);
  });
});
