import { HttpClient, HttpContext, HttpErrorResponse } from '@angular/common/http';
import { Injectable, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { SUPPRESS_ERROR_TOAST } from '../auth/error.interceptor';
import { ConfigService } from '../services/config.service';
import { WorkflowStartService } from '../services/workflows/workflow-start.service';
import { WorkflowRef } from '../services/workflows/workflow.models';
import { UserConnectorsService } from '../settings/connectors/services/user-connectors.service';
import { handoffStatusLine } from '../session/components/handoff/handoff-status-line';
import { ChatStateService } from '../session/services/chat/chat-state.service';
import { ToolInsightService } from '../session/services/chat/tool-insight.service';
import { HandoffStateService } from '../session/services/handoff/handoff-state.service';
import { WorkflowRunQueueService } from '../session/services/handoff/workflow-run-queue.service';
import { RunProgress, RunStepLine, runProgress, showsInThread } from '../session/services/handoff/workflow-run-progress';
import { WorkflowRunStateService } from '../session/services/handoff/workflow-run-state.service';
import { WorkflowRunView } from '../session/services/handoff/workflow-run.models';

/** A workflow's test run, as the designer remembers it: its source conversation, then its run. */
export interface TestRunRecord {
  /** The "Test: {title}" conversation the run's sheet opened in. */
  sessionId: string;
  /** Known once the sheet has created the run. */
  runId?: string;
}

/** What loading a test run by id found. */
export type TestRunLoad = 'loaded' | 'gone' | 'failed';

const STORAGE_KEY = 'aps.workflowTestRuns';

/** The run states the designer draws: started, whatever happened after. */
export function isStartedRun(run: WorkflowRunView | undefined): run is WorkflowRunView {
  return !!run && showsInThread(run);
}

/**
 * The designer's test run (docs/specs/workflow-designer.md §6.2, PR-D.4).
 *
 * Start test run opens the starter's sheet in the designer's panel, in a new conversation titled
 * "Test: {title}" (`WorkflowStartService.start` with `navigate: false`), so the tab stays on the
 * designer. Pressing Start on that sheet is the approval, and from then on it is an ordinary run:
 * the queue and the step streams are `providedIn: 'root'`, so leaving the designer mid-run is fine.
 *
 * This service remembers, per workflow, which conversation and run is its test run, so coming back
 * to the designer shows it again. The record is kept in `sessionStorage` too (this tab only, wrapped
 * in try/catch), so a reload can find the run by id: run state is otherwise hydrated only when a
 * source's messages load (`message-map.service.ts`), and the designer never loads them.
 *
 * Freshness: once the run is in `WorkflowRunStateService` (upserted at approve, or by `load`), the
 * panel and the canvas read each step from the same live handoff rows the run's row reads
 * (`HandoffStateService`, which the step streams and their end-of-turn re-reads keep current). There
 * is no polling.
 */
@Injectable({ providedIn: 'root' })
export class WorkflowTestRunService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly starter = inject(WorkflowStartService);
  private readonly runState = inject(WorkflowRunStateService);
  private readonly handoffState = inject(HandoffStateService);
  private readonly chatState = inject(ChatStateService);
  private readonly queue = inject(WorkflowRunQueueService);
  private readonly insight = inject(ToolInsightService);
  private readonly connectors = inject(UserConnectorsService);

  private readonly records = signal<ReadonlyMap<string, TestRunRecord>>(readStored());
  private readonly startingKeys = signal<ReadonlySet<string>>(new Set());

  recordFor(ref: WorkflowRef | null): TestRunRecord | undefined {
    return ref ? this.records().get(keyOf(ref)) : undefined;
  }

  isStarting(ref: WorkflowRef | null): boolean {
    return !!ref && this.startingKeys().has(keyOf(ref));
  }

  /** The test run's run, once it is in run state. */
  runFor(ref: WorkflowRef | null): WorkflowRunView | undefined {
    const record = this.recordFor(ref);
    if (!record?.runId) return undefined;
    return this.runState.runsFor(record.sessionId).find((run) => run.runId === record.runId);
  }

  /**
   * Open the sheet for a test run of `ref` in a new "Test: …" conversation, without navigating.
   * True once the sheet is up. A test run already recorded for `ref` is replaced; its conversations
   * stay where they are.
   */
  async begin(ref: WorkflowRef): Promise<boolean> {
    const key = keyOf(ref);
    if (this.startingKeys().has(key)) return false;
    this.startingKeys.update((set) => new Set(set).add(key));
    try {
      const sessionId = await this.starter.start(ref, { navigate: false, titlePrefix: 'Test: ' });
      if (!sessionId) return false;
      this.write(key, { sessionId });
      return true;
    } finally {
      this.startingKeys.update((set) => {
        const next = new Set(set);
        next.delete(key);
        return next;
      });
    }
  }

  /** The sheet created the run: remember its id, so a reload or a later visit can load it. */
  setRunId(ref: WorkflowRef, runId: string): void {
    const record = this.recordFor(ref);
    if (!record || record.runId === runId) return;
    this.write(keyOf(ref), { ...record, runId });
  }

  /** Stop showing the test run here. Its conversations and results stay where they are. */
  forget(ref: WorkflowRef): void {
    const key = keyOf(ref);
    if (!this.records().has(key)) return;
    this.records.update((map) => {
      const next = new Map(map);
      next.delete(key);
      return next;
    });
    this.persist();
  }

  /**
   * `GET /sessions/{id}/workflow-runs/{runId}` into run state, for a test run this tab doesn't hold
   * (after a reload). `gone` when it was never started (cancelled on the sheet, or the reload came
   * before Start) or no longer exists; the caller forgets it then.
   */
  async load(record: Required<TestRunRecord>): Promise<TestRunLoad> {
    const url = `${this.config.appApiUrl()}/sessions/${encodeURIComponent(record.sessionId)}/workflow-runs/${encodeURIComponent(record.runId)}`;
    try {
      const run = await firstValueFrom(
        this.http.get<WorkflowRunView>(url, { context: new HttpContext().set(SUPPRESS_ERROR_TOAST, true) }),
      );
      if (!isStartedRun(run)) return 'gone';
      // Another read may have put it there meanwhile; this tab's copy wins.
      if (!this.runState.runsFor(run.sessionId).some((r) => r.runId === run.runId)) this.runState.upsertRun(run);
      return 'loaded';
    } catch (error) {
      return error instanceof HttpErrorResponse && error.status === 404 ? 'gone' : 'failed';
    }
  }

  /** The run's progress exactly as its row computes it (`runProgress`), from the same live rows. */
  progress(run: WorkflowRunView): RunProgress {
    return runProgress(run, this.handoffState.rowsFor(run.sessionId), {
      isLoading: (target) => this.chatState.isSessionLoading(target),
      isPending: (handoffId) => this.queue.isPending(run.runId, handoffId),
    });
  }

  /** A step's phrase, worded as its line on the run's row words it (`handoffStatusLine`). */
  phrase(line: RunStepLine): string {
    if (line.state === 'queued') return 'Queued';
    if (line.state === 'not_started') return 'Not started';
    if (!line.row?.targetSessionId) return 'Didn’t start';
    const rowState = line.state === 'running' || line.state === 'needs_input' || line.state === 'done' ? line.state : 'stopped';
    return handoffStatusLine(line.row, rowState, this.insight, (providerId) => this.connectors.displayNameFor(providerId));
  }

  private write(key: string, record: TestRunRecord): void {
    this.records.update((map) => new Map(map).set(key, record));
    this.persist();
  }

  private persist(): void {
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(Object.fromEntries(this.records())));
    } catch {
      // Private mode or storage blocked: the test run is still shown for the life of the page.
    }
  }
}

function keyOf(ref: WorkflowRef): string {
  return `${ref.skillId}/${ref.name}`;
}

function readStored(): ReadonlyMap<string, TestRunRecord> {
  try {
    const raw = typeof sessionStorage === 'undefined' ? null : sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return new Map();
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return new Map();
    const entries = Object.entries(parsed as Record<string, unknown>).filter(
      (entry): entry is [string, TestRunRecord] => isRecord(entry[1]),
    );
    return new Map(entries);
  } catch {
    return new Map();
  }
}

function isRecord(value: unknown): value is TestRunRecord {
  if (!value || typeof value !== 'object') return false;
  const record = value as Partial<TestRunRecord>;
  return typeof record.sessionId === 'string' && (record.runId === undefined || typeof record.runId === 'string');
}
