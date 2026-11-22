import { HttpErrorResponse } from '@angular/common/http';
import { DestroyRef, Injectable, computed, effect, inject, signal, untracked } from '@angular/core';

import { ToastService } from '../../../services/toast/toast.service';
import { AgentHandoffService } from './agent-handoff.service';
import { HandoffStatus, HandoffView } from './handoff.models';
import { HandoffStateService } from './handoff-state.service';
import { MAX_PROMPT_STEPS_PER_CALL, PromptRefusedReason, PromptStepFrame, WorkflowPromptService } from './workflow-prompt.service';
import { WorkflowRunStateService } from './workflow-run-state.service';
import { DEFAULT_RUN_CONCURRENCY, RunEnvelope, RunStepLaunch, WorkflowRunView } from './workflow-run.models';
import { readStepsOf } from './workflow-run-progress';

/** A step stops holding a slot once its row reads one of these (spec §19 PR-W.4a). */
const SETTLED: ReadonlySet<HandoffStatus> = new Set(['done', 'needs_input', 'stopped', 'dismissed', 'abandoned']);

/**
 * A step a later one reads has settled once its row reads one of these (PR-D.5a): it will give
 * nothing more. Unlike a slot, `needs_input` hasn't: the user's answer is still to come.
 */
const READ_SETTLED: ReadonlySet<HandoffStatus> = new Set(['done', 'stopped', 'dismissed', 'abandoned']);

/**
 * `…/prepare` answers 409 until the server's row for every read step has settled. The tab sees a
 * step finish from its stream, a moment before the Runtime writes that row, so a 409 is retried
 * with a short, growing wait before the step is left "Not started" with Start.
 */
export const PREPARE_ATTEMPTS = 6;
export const PREPARE_RETRY_MS = 500;

/**
 * A prompt step the Runtime skipped as `reads_pending` (a step it reads hasn't settled on the
 * server yet, though this tab saw it finish) waits this long, growing, before it is sent again,
 * and is sent at most this many times in all before it is left "Not started" with Start.
 */
export const PROMPT_READS_ATTEMPTS = PREPARE_ATTEMPTS;
export const PROMPT_READS_RETRY_MS = PREPARE_RETRY_MS;

interface QueuedRun {
  envelope: RunEnvelope;
  /** Steps not started yet, in step order. */
  pending: readonly RunStepLaunch[];
  /** Handoff ids of the steps holding a slot. */
  running: readonly string[];
  /** Of `running`, the steps whose first message is being prepared, not yet sent (PR-D.5a). */
  preparing: readonly string[];
  /**
   * Handoff ids of the prompt steps on this run's one prompt invocation in flight, until each
   * one's frame arrives (PR-D.6c). They hold no Agent slot: the Runtime bounds its own calls.
   */
  prompting: readonly string[];
  /** Pending prompt steps waiting out a `reads_pending` retry: not ready until it is over. */
  held: readonly string[];
  /** How many times each prompt step has been skipped as `reads_pending`, by handoff id. */
  promptAttempts: ReadonlyMap<string, number>;
}

/** Where a step a pending one reads stands, as the queue sees it. */
type ReadState = 'settled' | 'live' | 'dead';

/**
 * The tab's queue for approved workflow runs (docs/specs/workflow-runs.md §6.1, PR-W.4a).
 *
 * At most `limits.concurrency` steps of a run start at once, each through the single handoff's
 * own background send (`AgentHandoffService.startRunStep`: no navigation, the user stays on the
 * source). The next starts when a running one settles: its row reaches `done`, `needs_input` or
 * `stopped`, or its stream ends without the row moving (a refused or undelivered send).
 *
 * Keyed by run id and provided in root, so it outlives the sheet and every route change; W.4b's
 * run row and W.4c's sidebar read it. A run lives as long as the tab (§6.1), so the page-unload
 * guard asks before closing a tab with queued or running steps.
 *
 * Every step is sent with the run's envelope as approved: exactly its `fileIds`, and the card as
 * reviewed. Both are on the run row (the card since PR-W.4b), so a reload in any browser can start
 * the steps this tab never started (`WorkflowRunService.resume`).
 *
 * `stop` closes a run's queue (Stop run): no pending step starts after it.
 *
 * **Stages (PR-D.5a).** A step that reads other steps waits in the queue until each has settled
 * (done or stopped), then takes the next free slot, in step order among the steps ready. Just
 * before its send it asks `…/prepare` for its first message, which carries the read steps' results.
 * A step that can no longer start here (it reads one that was never started in this tab and isn't
 * running anywhere) leaves the queue, and the run's row offers it as "Not started" with Start.
 *
 * **Prompt steps (PR-D.6c, workflow-designer §21.3).** A prompt step has no Agent and no
 * conversation, so it is never sent through a handoff's send. The run's ready prompt steps go
 * together on one `workflow_prompt` invocation (`WorkflowPromptService`), because one call warms
 * the documents' cached prefix for the rest; each settles from its own frame, and its row is read
 * then for its answer. They take none of the tab's Agent slots: the Runtime runs one invocation's
 * steps at the run's `limits.concurrency` itself. The tab keeps at most one prompt invocation in
 * flight per run, so that bound holds for the run as a whole; prompt steps that become ready
 * meanwhile go together on the next one, once every step on it has settled (a step's frame comes
 * after its call has ended). A step skipped as `reads_pending` waits
 * and is sent again a bounded number of times; one skipped as `not_open` is another tab's or
 * invocation's, so its row is re-read rather than resent.
 */
@Injectable({ providedIn: 'root' })
export class WorkflowRunQueueService {
  private readonly handoff = inject(AgentHandoffService);
  private readonly handoffState = inject(HandoffStateService);
  private readonly runState = inject(WorkflowRunStateService);
  private readonly prompts = inject(WorkflowPromptService);
  private readonly toast = inject(ToastService);

  private readonly runs = signal<ReadonlyMap<string, QueuedRun>>(new Map());

  /**
   * The page is going away (`pagehide`). Unloading aborts the running steps' streams, and each
   * aborted promise settles its slot; without this the queue would start the next step inside
   * the dying page, a send that may or may not reach the server. Starting nothing leaves those
   * steps `open`, so a reload shows them "Not started" with Start.
   */
  private unloading = false;

  /** True while any run in this tab has a step queued or running. */
  readonly hasActiveRun = computed(() =>
    [...this.runs().values()].some((run) => run.pending.length + run.running.length + run.prompting.length > 0),
  );

  constructor() {
    effect(() => {
      const runs = this.runs();
      for (const [runId, run] of runs) {
        const waiting = run.pending.some((step) => (step.reads?.length ?? 0) > 0);
        if (run.running.length === 0 && !waiting) continue;
        const rows = this.handoffState.rowsFor(run.envelope.sourceSessionId);
        for (const handoffId of run.running) {
          if (run.preparing.includes(handoffId)) continue;
          const row = rows.find((candidate) => candidate.handoffId === handoffId);
          if (row && SETTLED.has(row.status)) untracked(() => this.settle(runId, handoffId));
        }
        // A step waiting on what it reads starts once those rows settle, here or on a reload's replay.
        if (waiting) {
          this.runState.runsFor(run.envelope.sourceSessionId);
          untracked(() => this.pump(runId));
        }
      }
    });

    if (typeof window !== 'undefined') {
      const guard = (event: BeforeUnloadEvent) => {
        if (!this.hasActiveRun()) return;
        event.preventDefault();
        // Older browsers need a returnValue to show the prompt.
        event.returnValue = '';
      };
      const hide = () => {
        this.unloading = true;
      };
      // A page restored from the back/forward cache is alive again: carry on where it stopped.
      const show = (event: PageTransitionEvent) => {
        if (!event.persisted || !this.unloading) return;
        this.unloading = false;
        for (const runId of this.runs().keys()) this.pump(runId);
      };
      window.addEventListener('beforeunload', guard);
      window.addEventListener('pagehide', hide);
      window.addEventListener('pageshow', show);
      inject(DestroyRef).onDestroy(() => {
        window.removeEventListener('beforeunload', guard);
        window.removeEventListener('pagehide', hide);
        window.removeEventListener('pageshow', show);
      });
    }
  }

  /** The source conversations of every run with steps in this tab's queue. */
  queuedSources(): string[] {
    return [...new Set([...this.runs().values()].map((run) => run.envelope.sourceSessionId))];
  }

  /** True while this run has steps in the tab's queue (queued or running). */
  isQueued(runId: string): boolean {
    return this.runs().has(runId);
  }

  /** Steps of this run still waiting for a slot. */
  pendingCount(runId: string): number {
    return this.runs().get(runId)?.pending.length ?? 0;
  }

  /** This step is queued in this tab, waiting for a slot, for what it reads, or for its first message. */
  isPending(runId: string, handoffId: string): boolean {
    const run = this.runs().get(runId);
    if (!run) return false;
    return run.pending.some((step) => step.handoffId === handoffId) || run.preparing.includes(handoffId);
  }

  /** This prompt step is on this tab's prompt invocation, and its frame hasn't arrived yet. */
  isRunning(runId: string, handoffId: string): boolean {
    return this.runs().get(runId)?.prompting.includes(handoffId) ?? false;
  }

  /**
   * Stop run: close this run's queue, so no pending step starts. The running steps are stopped by
   * the caller, each with its own cancel; their slots no longer matter. A prompt invocation in
   * flight needs nothing here: the Runtime re-reads the run row, and on `stopped` cancels its calls
   * and stops each claimed step `user_stopped`, whose frames this tab then reads as usual.
   */
  stop(runId: string): void {
    this.finish(runId);
  }

  /**
   * Queue an approved run's steps and start the first `concurrency` of them. A step already
   * queued or running for this run is not queued twice.
   */
  enqueue(envelope: RunEnvelope, steps: readonly RunStepLaunch[]): void {
    const current = this.runs().get(envelope.runId);
    const known = new Set([
      ...(current?.pending ?? []).map((s) => s.handoffId),
      ...(current?.running ?? []),
      ...(current?.prompting ?? []),
    ]);
    const fresh = steps.filter((step) => !known.has(step.handoffId));
    if (!current && fresh.length === 0) return;
    this.setRun(envelope.runId, {
      envelope,
      pending: [...(current?.pending ?? []), ...fresh],
      running: current?.running ?? [],
      preparing: current?.preparing ?? [],
      prompting: current?.prompting ?? [],
      held: current?.held ?? [],
      // A Start from the run row is a fresh try for a step that ran out of retries.
      promptAttempts: withoutKeys(current?.promptAttempts, fresh.map((step) => step.handoffId)),
    });
    this.pump(envelope.runId);
  }

  /**
   * The steps of an approved run that were never started (spec §19 PR-W.4a "Not started"): open
   * rows with no target conversation, not removed, and not in this tab's queue.
   */
  unstartedSteps(run: WorkflowRunView): RunStepLaunch[] {
    if (run.status !== 'approved') return [];
    const queued = this.runs().get(run.runId);
    const held = new Set([...(queued?.pending ?? []).map((s) => s.handoffId), ...(queued?.running ?? []), ...(queued?.prompting ?? [])]);
    const live = this.handoffState.rowsFor(run.sessionId);
    return run.steps
      .filter((step) => !step.removed && !held.has(step.handoffId))
      .filter((step) => {
        const row = live.find((candidate) => candidate.handoffId === step.handoffId) ?? step.handoff;
        return !!row && row.status === 'open' && !row.targetSessionId;
      })
      .map((step) => launchOf(run, step));
  }

  private pump(runId: string): void {
    // Nothing starts while the page unloads, and the run stays as it is (envelope included).
    if (this.unloading) return;
    const run = this.runs().get(runId);
    if (!run) return;
    const pending = this.startable(run);
    const limit = Math.max(1, run.envelope.concurrency || DEFAULT_RUN_CONCURRENCY);
    const free = limit - run.running.length;
    // Step order among the steps whose reads have settled; a step reading nothing is always ready.
    const ready = pending.filter(
      (step) => !run.held.includes(step.handoffId) && (step.reads ?? []).every((id) => this.readState(run, id, pending) === 'settled'),
    );
    const starting = free > 0 ? ready.filter((step) => step.kind !== 'prompt').slice(0, free) : [];
    // Every ready prompt step together, on the run's one prompt invocation, once no step is in flight.
    const prompting =
      run.prompting.length === 0 ? ready.filter((step) => step.kind === 'prompt').slice(0, MAX_PROMPT_STEPS_PER_CALL) : [];
    if (starting.length === 0 && prompting.length === 0 && pending.length === run.pending.length) {
      if (isIdle(run)) this.finish(runId);
      return;
    }
    const started = new Set([...starting, ...prompting].map((step) => step.handoffId));
    const next: QueuedRun = {
      ...run,
      pending: pending.filter((step) => !started.has(step.handoffId)),
      running: [...run.running, ...starting.map((step) => step.handoffId)],
      preparing: [...run.preparing, ...starting.filter((step) => step.reads?.length).map((step) => step.handoffId)],
      prompting: [...run.prompting, ...prompting.map((step) => step.handoffId)],
    };
    if (isIdle(next)) {
      this.finish(runId);
      return;
    }
    this.setRun(runId, next);
    for (const step of starting) this.start(run.envelope, step);
    if (prompting.length) void this.runPrompts(run.envelope, prompting);
  }

  /**
   * The pending steps that can still start in this tab. One that reads a step which will never
   * settle here (never started, not queued, not running anywhere) leaves the queue, and so does
   * anything reading it; the run's row shows it "Not started", and Start queues it again.
   */
  private startable(run: QueuedRun): readonly RunStepLaunch[] {
    let pending = run.pending;
    for (;;) {
      const kept = pending.filter((step) => !(step.reads ?? []).some((id) => this.readState(run, id, pending) === 'dead'));
      if (kept.length === pending.length) return pending;
      pending = kept;
    }
  }

  private readState(run: QueuedRun, handoffId: string, pending: readonly RunStepLaunch[]): ReadState {
    if (pending.some((step) => step.handoffId === handoffId)) return 'live';
    const source = run.envelope.sourceSessionId;
    const view = this.runState
      .runsFor(source)
      .find((candidate) => candidate.runId === run.envelope.runId)
      ?.steps.find((step) => step.handoffId === handoffId);
    if (view?.removed) return 'settled';
    const row = this.handoffState.rowsFor(source).find((candidate) => candidate.handoffId === handoffId) ?? view?.handoff ?? null;
    if (row && READ_SETTLED.has(row.status)) return 'settled';
    if (run.running.includes(handoffId) || run.prompting.includes(handoffId)) return 'live';
    // A step whose row is gone gives nothing more; one this tab knows nothing about may yet start.
    if (!row) return view ? 'settled' : 'live';
    if (row.status !== 'open' || row.targetSessionId) return 'live';
    return 'dead';
  }

  private start(envelope: RunEnvelope, step: RunStepLaunch): void {
    if (step.reads?.length) {
      void this.prepareThenSend(envelope, step);
      return;
    }
    this.send(envelope, step);
  }

  /**
   * Ask app-api for the step's first message (the read steps' results, then its request), then
   * send it. Written before the send, never on a turn's path. A step that can't be prepared frees
   * its slot and stays "Not started"; one whose run was stopped meanwhile is not sent.
   */
  private async prepareThenSend(envelope: RunEnvelope, step: RunStepLaunch): Promise<void> {
    let message: string | undefined;
    for (let attempt = 0; attempt < PREPARE_ATTEMPTS && message === undefined; attempt++) {
      try {
        message = (await this.handoff.prepareRunStep(envelope.sourceSessionId, envelope.runId, step.stepId)).message;
      } catch (error) {
        const settling = error instanceof HttpErrorResponse && error.status === 409;
        if (!settling || attempt === PREPARE_ATTEMPTS - 1) {
          console.error('Workflow run step could not be prepared:', error);
          break;
        }
        await delay(PREPARE_RETRY_MS * (attempt + 1));
      }
      if (!this.runs().has(envelope.runId)) return;
    }
    const run = this.runs().get(envelope.runId);
    if (!run) return;
    if (this.unloading) {
      // Not sent: back to the head of the queue, so a page restored from the cache starts it again.
      this.setRun(envelope.runId, {
        ...run,
        pending: [step, ...run.pending],
        running: run.running.filter((id) => id !== step.handoffId),
        preparing: run.preparing.filter((id) => id !== step.handoffId),
      });
      return;
    }
    this.setRun(envelope.runId, { ...run, preparing: run.preparing.filter((id) => id !== step.handoffId) });
    if (message === undefined) {
      this.settle(envelope.runId, step.handoffId);
      return;
    }
    this.send(envelope, step, message);
  }

  private send(envelope: RunEnvelope, step: RunStepLaunch, message?: string): void {
    let sent: Promise<void>;
    try {
      sent = this.handoff.startRunStep({
        sourceSessionId: envelope.sourceSessionId,
        handoffId: step.handoffId,
        agentId: step.agentId,
        agentName: step.agentName,
        request: step.request,
        ...(message !== undefined ? { message } : {}),
        sourceMessageIndex: envelope.sourceMessageIndex,
        excerptTokens: envelope.excerptTokens,
        turnsCovered: envelope.turnsCovered,
        sourceTitle: envelope.sourceTitle,
        contextText: envelope.contextText,
        // Exactly the run's approved files, on every step: nothing on the server checks a step's
        // `fileIds` against the run's list in step 1 (spec §19 PR-W.1, "For W.4a").
        sharedFileIds: [...envelope.fileIds],
        filesUnticked: envelope.filesUnticked,
        fileUploadIds: envelope.fileUploadIds,
        invokedSkillIds: envelope.invokedSkillIds,
        folders: envelope.folders,
        runId: envelope.runId,
        stepId: step.stepId,
      });
    } catch (error) {
      console.error('Workflow run step failed to start:', error);
      this.settle(envelope.runId, step.handoffId);
      return;
    }
    // The stream ended (or never began): the slot is free whatever the row says next.
    sent
      .catch((error) => console.error('Workflow run step send failed:', error))
      .finally(() => this.settle(envelope.runId, step.handoffId));
  }

  /**
   * Send these ready prompt steps on one `workflow_prompt` invocation, and settle each from its
   * frame. A step the stream closed on without a frame may still run on the Runtime (a dropped
   * connection doesn't cancel), so its row is re-read; the run refresh follows it from there.
   */
  private async runPrompts(envelope: RunEnvelope, steps: readonly RunStepLaunch[]): Promise<void> {
    const byStepId = new Map(steps.map((step) => [step.stepId, step]));
    const answered = new Set<string>();
    let refused: PromptRefusedReason | null = null;
    await this.prompts.run(
      envelope.sourceSessionId,
      envelope.runId,
      steps.map((step) => step.stepId),
      {
        onStep: (frame) => {
          const step = byStepId.get(frame.stepId);
          if (!step) return;
          answered.add(step.handoffId);
          this.onPromptFrame(envelope, step, frame);
        },
        onRefused: (reason) => {
          refused = reason;
        },
      },
    );
    const unanswered = steps.filter((step) => !answered.has(step.handoffId));
    const run = this.runs().get(envelope.runId);
    if (run) {
      const gone = new Set(unanswered.map((step) => step.handoffId));
      this.setRun(envelope.runId, { ...run, prompting: run.prompting.filter((id) => !gone.has(id)) });
    }
    if (refused) this.sayRefused(refused);
    // Refused before any claim: the rows are untouched and open, so they read "Not started".
    else for (const step of unanswered) void this.rereadRow(envelope.sourceSessionId, step.handoffId);
    this.pump(envelope.runId);
  }

  private onPromptFrame(envelope: RunEnvelope, step: RunStepLaunch, frame: PromptStepFrame): void {
    const run = this.runs().get(envelope.runId);
    if (!run) {
      // Stop run closed the queue meanwhile: the frame still says how the step ended.
      if (frame.status !== 'skipped') this.settlePromptRow(envelope, step, frame);
      return;
    }
    const prompting = run.prompting.filter((id) => id !== step.handoffId);
    if (frame.status !== 'skipped') {
      this.setRun(envelope.runId, { ...run, prompting });
      this.settlePromptRow(envelope, step, frame);
      return;
    }
    switch (frame.reason) {
      case 'reads_pending': {
        // This tab saw a read finish a moment before the Runtime wrote its row. Wait, then send
        // it again, a bounded number of times: never a loop.
        const attempts = (run.promptAttempts.get(step.handoffId) ?? 0) + 1;
        if (attempts >= PROMPT_READS_ATTEMPTS) {
          this.setRun(envelope.runId, { ...run, prompting });
          return;
        }
        this.setRun(envelope.runId, {
          ...run,
          prompting,
          pending: [...run.pending, step],
          held: [...run.held, step.handoffId],
          promptAttempts: new Map(run.promptAttempts).set(step.handoffId, attempts),
        });
        setTimeout(() => this.release(envelope.runId, step.handoffId), PROMPT_READS_RETRY_MS * attempts);
        return;
      }
      case 'not_open':
        // Another tab or invocation has it: read where it stands rather than send it again.
        this.setRun(envelope.runId, { ...run, prompting });
        void this.rereadRow(envelope.sourceSessionId, step.handoffId);
        return;
      default:
        // Not this run's prompt step, or a read it can't be given: it stays "Not started".
        console.warn('Workflow prompt step was not started:', frame.reason);
        this.setRun(envelope.runId, { ...run, prompting });
    }
  }

  /** A held prompt step's wait is over: it is ready again once its reads read settled. */
  private release(runId: string, handoffId: string): void {
    const run = this.runs().get(runId);
    if (!run || !run.held.includes(handoffId)) return;
    this.setRun(runId, { ...run, held: run.held.filter((id) => id !== handoffId) });
    this.pump(runId);
  }

  /**
   * A prompt step settled: show it at once from its frame, then read its row for its answer. The
   * row read is authoritative (it replaces the tab's copy outright), never on a turn's path.
   */
  private settlePromptRow(envelope: RunEnvelope, step: RunStepLaunch, frame: Exclude<PromptStepFrame, { status: 'skipped' }>): void {
    const known = this.knownRow(envelope.sourceSessionId, envelope.runId, step.handoffId);
    if (known) {
      this.handoffState.upsertRow({
        ...known,
        status: frame.status,
        ...(frame.status === 'stopped' ? { stoppedReason: frame.stoppedReason } : { stoppedReason: undefined }),
      });
    }
    void this.rereadRow(envelope.sourceSessionId, step.handoffId);
  }

  private knownRow(sourceSessionId: string, runId: string, handoffId: string): HandoffView | null {
    const live = this.handoffState.rowsFor(sourceSessionId).find((row) => row.handoffId === handoffId);
    if (live) return live;
    const view = this.runState
      .runsFor(sourceSessionId)
      .find((run) => run.runId === runId)
      ?.steps.find((s) => s.handoffId === handoffId);
    return view?.handoff ?? null;
  }

  private async rereadRow(sourceSessionId: string, handoffId: string): Promise<void> {
    const row = await this.handoff.readRow(sourceSessionId, handoffId);
    if (row) this.handoffState.upsertRow(row);
  }

  private sayRefused(reason: PromptRefusedReason): void {
    // A run stopped meanwhile (`not_approved`) needs no word: its row already says so.
    if (reason === 'not_approved') return;
    this.toast.error(
      'Prompt steps couldn’t start',
      reason === 'flag_off' ? 'Prompt steps aren’t available here yet.' : 'Try Start on the run again.',
    );
  }

  /** A step stopped holding its slot. Idempotent: a row settling and its stream ending both land here. */
  private settle(runId: string, handoffId: string): void {
    const run = this.runs().get(runId);
    if (!run || !run.running.includes(handoffId)) return;
    this.setRun(runId, {
      ...run,
      running: run.running.filter((id) => id !== handoffId),
      preparing: run.preparing.filter((id) => id !== handoffId),
    });
    this.pump(runId);
  }

  private finish(runId: string): void {
    this.runs.update((map) => {
      if (!map.has(runId)) return map;
      const next = new Map(map);
      next.delete(runId);
      return next;
    });
  }

  private setRun(runId: string, run: QueuedRun): void {
    this.runs.update((map) => new Map(map).set(runId, run));
  }
}

function withoutKeys(map: ReadonlyMap<string, number> | undefined, keys: readonly string[]): ReadonlyMap<string, number> {
  const next = new Map(map ?? []);
  for (const key of keys) next.delete(key);
  return next;
}

/** Nothing queued, running or on a prompt invocation: the run's queue can close. */
function isIdle(run: Pick<QueuedRun, 'pending' | 'running' | 'prompting'>): boolean {
  return run.pending.length === 0 && run.running.length === 0 && run.prompting.length === 0;
}

/** A run row's step as the queue starts it: its worker, and the handoff ids of the steps it reads. */
export function launchOf(run: WorkflowRunView, step: WorkflowRunView['steps'][number]): RunStepLaunch {
  return {
    stepId: step.stepId,
    handoffId: step.handoffId,
    ...(step.kind === 'prompt' ? { kind: 'prompt' as const, modelId: step.modelId } : {}),
    agentId: step.agentId,
    agentName: step.agentName,
    request: step.request,
    reads: readStepsOf(run, step).map((read) => read.handoffId),
  };
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
