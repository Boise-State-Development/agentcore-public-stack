import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, inject, untracked } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { ConfigService } from '../../../services/config.service';
import { FileUploadService } from '../../../services/file-upload/file-upload.service';
import { FEATURES } from '../../../services/features';
import { ToastService } from '../../../services/toast/toast.service';
import { ChatHttpService } from '../chat/chat-http.service';
import { ChatRequestService } from '../chat/chat-request.service';
import { ChatStateService } from '../chat/chat-state.service';
import { Message } from '../models/message.model';
import { ComposerDraftService } from '../session/composer-draft.service';
import { MessageMapService } from '../session/message-map.service';
import { SessionService } from '../session/session.service';
import { AgentHandoffService } from './agent-handoff.service';
import { HandoffBriefService } from './handoff-brief.service';
import { HandoffStateService } from './handoff-state.service';
import { BriefFallbackReason, HandoffReview, MAX_SHARED_FILES, SHARED_FILES_RECENT_TURNS } from './handoff.models';
import { WorkflowRunQueueService } from './workflow-run-queue.service';
import { WorkflowRunResultService } from './workflow-run-result.service';
import { WorkflowRunStateService } from './workflow-run-state.service';
import {
  CreateWorkflowRunResponse,
  DEFAULT_RUN_CONCURRENCY,
  RunDraft,
  RunEnvelope,
  RunReview,
  RunReviewStep,
  RunShareableFile,
  RunStepLaunch,
  WorkflowRunCard,
  WorkflowRunView,
} from './workflow-run.models';

/**
 * Starting a workflow run (docs/specs/workflow-runs.md §4, §19 PR-W.4a, §20.1).
 *
 * Mentioning two or more Agents in one message opens the run sheet instead of the single review
 * sheet. `open` asks app-api for the run (one `open` handoff row per step) and the one context card
 * every step gets, and, with `features.handoffBrief`, streams one brief for the whole run into it.
 * `start` approves the envelope as reviewed and hands the kept steps to `WorkflowRunQueueService`,
 * which starts them four at a time. `cancel` dismisses the run and gives the request back to the
 * composer, as the single sheet does.
 *
 * The single sheet's path (`AgentHandoffService.open`/`send`) is untouched: a run only adds this
 * service beside it, and its steps go through the single handoff's own send.
 *
 * After Start (PR-W.4b): `resume` starts the steps a reload left unstarted from the run row's own
 * envelope (its files and its card as reviewed), in any browser; `stop` is Stop run.
 */
@Injectable({ providedIn: 'root' })
export class WorkflowRunService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly state = inject(WorkflowRunStateService);
  private readonly queue = inject(WorkflowRunQueueService);
  private readonly handoff = inject(AgentHandoffService);
  private readonly chatRequest = inject(ChatRequestService);
  private readonly briefs = inject(HandoffBriefService);
  private readonly composerDraft = inject(ComposerDraftService);
  private readonly fileUpload = inject(FileUploadService);
  private readonly messageMap = inject(MessageMapService);
  private readonly toast = inject(ToastService);
  private readonly features = inject(FEATURES);
  private readonly results = inject(WorkflowRunResultService);
  private readonly handoffState = inject(HandoffStateService);
  private readonly chatState = inject(ChatStateService);
  private readonly chatHttp = inject(ChatHttpService);
  private readonly sessions = inject(SessionService);

  /** Open the run sheet for a message that mentioned several Agents. Nothing is sent. */
  async open(sourceSessionId: string, draft: RunDraft): Promise<void> {
    const previous = this.state.reviewFor(sourceSessionId);
    if (previous) this.discard(sourceSessionId, previous);
    this.state.setReview(sourceSessionId, {
      draft,
      phase: 'loading',
      request: draft.request,
      steps: [],
      card: '',
      cardRemoved: false,
      fileIds: [],
    });
    void this.loadShareableFiles(sourceSessionId);
    try {
      const created = await firstValueFrom(
        this.http.post<CreateWorkflowRunResponse>(this.runsUrl(sourceSessionId), {
          request: draft.request,
          steps: draft.agents.map((agent) => ({ agentId: agent.agentId })),
        }),
      );
      if (this.state.reviewFor(sourceSessionId)?.draft !== draft) {
        // Cancelled (or replaced) while the card was loading: dismiss what the server just opened.
        void this.dismiss(sourceSessionId, created.run.runId);
        return;
      }
      const brief = this.features.handoffBrief && created.steps.length ? ({ status: 'writing', text: '' } as const) : undefined;
      this.state.updateReview(sourceSessionId, {
        phase: 'ready',
        created,
        request: created.run.request,
        steps: created.steps.map(
          (step): RunReviewStep => ({
            stepId: step.stepId,
            handoffId: step.handoffId,
            agentId: step.agentId,
            agentName: step.agentName,
            ownRequest: step.request === created.run.request ? null : step.request,
            removed: false,
            modelId: step.modelId,
            inputPricePerMillionTokens: step.inputPricePerMillionTokens,
          }),
        ),
        card: created.excerpt,
        brief: brief ? { ...brief } : undefined,
      });
      if (brief) void this.requestBrief(sourceSessionId, created, draft);
    } catch (error) {
      this.state.updateReview(sourceSessionId, { phase: 'error', error: describeError(error) });
    }
  }

  setRequest(sourceSessionId: string, request: string): void {
    this.state.updateReview(sourceSessionId, { request });
  }

  /** Give one step its own request, or (`null`) put it back on the shared one. */
  setStepRequest(sourceSessionId: string, stepId: string, request: string | null): void {
    this.updateStep(sourceSessionId, stepId, { ownRequest: request });
  }

  /** Remove a step from the run. The last one stays: a run of one still starts (§20.1). */
  removeStep(sourceSessionId: string, stepId: string): void {
    const review = this.state.reviewFor(sourceSessionId);
    if (!review || keptSteps(review).length <= 1) return;
    this.updateStep(sourceSessionId, stepId, { removed: true });
  }

  setCard(sourceSessionId: string, card: string): void {
    this.state.updateReview(sourceSessionId, { card });
  }

  setCardRemoved(sourceSessionId: string, removed: boolean): void {
    this.state.updateReview(sourceSessionId, { cardRemoved: removed });
  }

  /** Tick or untick one of the source's files. At most `MAX_SHARED_FILES` go, to every step. */
  setFileShared(sourceSessionId: string, uploadId: string, shared: boolean): void {
    const review = this.state.reviewFor(sourceSessionId);
    if (!review) return;
    const rest = review.fileIds.filter((id) => id !== uploadId);
    if (shared && rest.length >= MAX_SHARED_FILES) return;
    const order = (review.files ?? []).map((f) => f.uploadId);
    const next = shared ? [...rest, uploadId] : rest;
    this.state.updateReview(sourceSessionId, {
      fileIds: next.sort((a, b) => order.indexOf(a) - order.indexOf(b)),
    });
  }

  /** Stop waiting for the brief and keep the excerpt in the card. */
  useExcerpt(sourceSessionId: string): void {
    if (this.state.reviewFor(sourceSessionId)?.brief?.status !== 'writing') return;
    this.settleBriefFallback(sourceSessionId, 'preempted');
    void this.briefs.preempt(sourceSessionId);
  }

  /**
   * The full first message one step's Agent will receive, exactly as the server builds it. The
   * single sheet's own builder, given this step's request and handoff id (its file lines name the
   * step's reference rows).
   */
  previewPrompt(review: RunReview, stepId: string): string {
    const step = review.steps.find((candidate) => candidate.stepId === stepId);
    const created = review.created;
    if (!step || !created) return '';
    const request = stepRequest(review, step);
    const single: HandoffReview = {
      draft: { agentId: step.agentId, agentName: step.agentName, request },
      phase: 'ready',
      request,
      card: review.card,
      cardRemoved: review.cardRemoved,
      files: review.files,
      fileIds: review.fileIds,
      created: {
        handoffId: step.handoffId,
        excerpt: created.excerpt,
        excerptHash: created.excerptHash,
        excerptTokens: created.excerptTokens,
        turnsCovered: created.turnsCovered,
        contextBlock: created.contextBlock,
        prompt: '',
        request,
        sourceMessageIndex: created.sourceMessageIndex,
        sourceTitle: created.sourceTitle,
      },
    };
    return this.handoff.previewPrompt(single);
  }

  /** Close the sheet without starting: dismiss the run and give the request back to the composer. */
  cancel(sourceSessionId: string): void {
    const review = this.state.reviewFor(sourceSessionId);
    if (!review) return;
    this.discard(sourceSessionId, review);
    const text = review.request.trim();
    if (text) this.composerDraft.request(sourceSessionId, text);
  }

  /**
   * Start: approve the envelope as reviewed, then queue the kept steps. Every step is sent with
   * the approved files and the card exactly as the sheet shows it.
   */
  async start(sourceSessionId: string): Promise<void> {
    const review = this.state.reviewFor(sourceSessionId);
    const created = review?.created;
    if (!review || !created || !canStart(review)) return;
    this.state.updateReview(sourceSessionId, { phase: 'starting', error: undefined });

    const shared = review.request.trim();
    const filesUnticked = Math.max(0, (review.files?.length ?? 0) - review.fileIds.length);
    // The card as reviewed goes on the run row, so a reload in any browser starts the steps left
    // with exactly this card (PR-W.4b).
    const card: WorkflowRunCard = review.cardRemoved ? { removed: true } : { text: review.card, removed: false };
    let approved: WorkflowRunView;
    try {
      approved = await firstValueFrom(
        this.http.post<WorkflowRunView>(`${this.runsUrl(sourceSessionId)}/${encodeURIComponent(created.run.runId)}/approve`, {
          request: shared,
          steps: review.steps.map((step) =>
            step.removed
              ? { stepId: step.stepId, removed: true }
              : step.ownRequest !== null
                ? { stepId: step.stepId, request: step.ownRequest.trim() }
                : { stepId: step.stepId },
          ),
          fileIds: review.fileIds,
          filesUnticked,
          card,
        }),
      );
    } catch (error) {
      this.state.updateReview(sourceSessionId, { phase: 'ready', error: describeApproveError(error) });
      return;
    }

    const envelope: RunEnvelope = {
      runId: approved.runId,
      sourceSessionId,
      sourceTitle: created.sourceTitle,
      sourceMessageIndex: created.sourceMessageIndex,
      excerptTokens: created.excerptTokens,
      turnsCovered: created.turnsCovered,
      contextText: review.cardRemoved ? undefined : review.card,
      fileIds: approved.fileIds ?? review.fileIds,
      filesUnticked,
      fileUploadIds: review.draft.fileUploadIds,
      invokedSkillIds: review.draft.invokedSkillIds,
      folders: review.draft.folders,
      concurrency: approved.limits?.concurrency ?? DEFAULT_RUN_CONCURRENCY,
    };
    // The approved row is the truth for each step's request and whether it was kept.
    const steps: RunStepLaunch[] = approved.steps
      .filter((step) => !step.removed)
      .map((step) => ({
        stepId: step.stepId,
        handoffId: step.handoffId,
        agentId: step.agentId,
        agentName: step.agentName,
        request: step.request,
      }));

    this.state.upsertRun(approved);
    this.state.clearReview(sourceSessionId);
    this.queue.enqueue(envelope, steps);
    this.results.watch(approved);
    this.toast.info(`Handed off to ${agentsLabel(steps.length)}`);
  }

  /**
   * Start an approved run's steps that were never started (after a reload, §19 PR-W.4a), from the
   * run row's own envelope: its approved files and its card as reviewed. Works in any browser.
   * False when the row has no card (approved before PR-W.4b) or nothing is left to start.
   *
   * What the row doesn't keep: the mention message's own attachments, skills and folders, which
   * only the steps the approving tab started carried, and the unticked-file count (a metric).
   */
  resume(run: WorkflowRunView): boolean {
    const steps = this.queue.unstartedSteps(run);
    if (!canResume(run) || steps.length === 0) return false;
    const listed = steps
      .map((step) => run.steps.find((s) => s.handoffId === step.handoffId)?.handoff)
      .find((row) => !!row);
    const envelope: RunEnvelope = {
      runId: run.runId,
      sourceSessionId: run.sessionId,
      sourceTitle: this.sourceTitle(run.sessionId),
      sourceMessageIndex: run.sourceMessageIndex,
      excerptTokens: listed?.excerptTokens ?? 0,
      turnsCovered: listed?.turnsCovered,
      contextText: run.card?.removed ? undefined : run.card?.text,
      fileIds: [...(run.fileIds ?? [])],
      filesUnticked: 0,
      concurrency: run.limits?.concurrency ?? DEFAULT_RUN_CONCURRENCY,
    };
    this.queue.enqueue(envelope, steps);
    this.results.watch(run);
    this.toast.info(`Handed off to ${agentsLabel(steps.length)}`);
    return true;
  }

  /**
   * Stop run (§8, §20.2): close the run on the server so no queued step starts anywhere, close
   * this tab's queue, then stop each running step with its own cooperative cancel, as Stop in its
   * conversation would. No confirmation: finished steps keep their results, and a stopped step has
   * Continue in its conversation.
   */
  async stop(run: WorkflowRunView): Promise<void> {
    this.queue.stop(run.runId);
    try {
      await firstValueFrom(this.http.post<void>(`${this.runsUrl(run.sessionId)}/${encodeURIComponent(run.runId)}/stop`, {}));
    } catch (error) {
      // 409: never approved. Anything else: the queue here is closed either way; say so.
      if (!(error instanceof HttpErrorResponse && error.status === 409)) {
        this.toast.error('The run couldn’t be stopped everywhere', 'Steps already running here were stopped. Try again.');
      }
    }
    const current = this.state.runsFor(run.sessionId).find((r) => r.runId === run.runId) ?? run;
    this.state.upsertRun({ ...current, status: 'stopped' });
    // Every step streaming in this tab: the ones holding a queue slot, and any resumed from its
    // own conversation since. A step running in another tab stops when that tab's Stop is pressed.
    const rows = this.handoffState.rowsFor(run.sessionId);
    for (const step of run.steps) {
      const target = (rows.find((row) => row.handoffId === step.handoffId) ?? step.handoff)?.targetSessionId;
      if (target && this.chatState.isSessionLoading(target)) this.chatHttp.cancelChatRequest(target);
    }
  }

  private discard(sourceSessionId: string, review: RunReview): void {
    this.state.clearReview(sourceSessionId);
    // A brief still writing holds the source's lease: stop it the cancel-only way, never Stop's.
    if (review.brief?.status === 'writing') void this.briefs.preempt(sourceSessionId);
    if (review.created) void this.dismiss(sourceSessionId, review.created.run.runId);
  }

  private updateStep(sourceSessionId: string, stepId: string, patch: Partial<RunReviewStep>): void {
    const review = this.state.reviewFor(sourceSessionId);
    if (!review) return;
    this.state.updateReview(sourceSessionId, {
      steps: review.steps.map((step) => (step.stepId === stepId ? { ...step, ...patch } : step)),
    });
  }

  /**
   * One brief for the whole run (§6.3), streamed into the one card. Sent as the first step's
   * handoff with the run id; the Runtime's run variant (PR-W.3) names every step's Agent, and
   * before it lands the Runtime ignores `runId` and writes a single brief. Every frame is checked
   * against the sheet it was started for.
   */
  private async requestBrief(sourceSessionId: string, created: CreateWorkflowRunResponse, draft: RunDraft): Promise<void> {
    const runId = created.run.runId;
    const handoffId = created.steps[0].handoffId;
    const writing = (): RunReview | undefined => {
      const review = this.state.reviewFor(sourceSessionId);
      return review?.created?.run.runId === runId && review.brief?.status === 'writing' ? review : undefined;
    };

    let body: Record<string, unknown>;
    try {
      // A brief from an earlier sheet on this thread would hold the lease and answer `busy`.
      await this.briefs.preempt(sourceSessionId);
      body = await this.chatRequest.buildHandoffBriefRequest({
        sourceSessionId,
        handoffId,
        runId,
        request: created.run.request || created.steps[0].request,
        sourceAssistantId: draft.sourceAssistantId,
      });
    } catch {
      if (writing()) this.settleBriefFallback(sourceSessionId, 'error');
      return;
    }
    if (!writing()) return;

    await this.briefs.stream(sourceSessionId, handoffId, body, {
      onText: (text) => {
        const review = writing();
        if (!review?.brief) return;
        this.state.updateReview(sourceSessionId, { brief: { ...review.brief, text: review.brief.text + text } });
      },
      onFallback: (reason) => {
        if (writing()) this.settleBriefFallback(sourceSessionId, reason);
      },
      onDone: ({ briefHash, cacheStatus }) => {
        const review = writing();
        if (!review?.brief) return;
        const text = review.brief.text.trim();
        if (!text) {
          this.settleBriefFallback(sourceSessionId, 'error');
          return;
        }
        this.state.updateReview(sourceSessionId, {
          card: text,
          brief: { status: 'written', text, briefHash, cacheStatus },
        });
      },
    });
  }

  /** No brief: discard whatever streamed and keep the excerpt in the card. */
  private settleBriefFallback(sourceSessionId: string, reason: BriefFallbackReason): void {
    const review = this.state.reviewFor(sourceSessionId);
    if (!review?.created) return;
    this.state.updateReview(sourceSessionId, {
      card: review.created.excerpt,
      brief: { status: 'fallback', text: '', fallbackReason: reason },
    });
  }

  /**
   * List the source's files for the sheet's checkboxes, as the single sheet does (§4.9), with
   * each one's token count for the estimate. A failed listing leaves the sheet without files.
   */
  private async loadShareableFiles(sourceSessionId: string): Promise<void> {
    const review = this.state.reviewFor(sourceSessionId);
    let files: RunShareableFile[];
    try {
      const listed = await this.fileUpload.listSessionFiles(sourceSessionId);
      files = listed
        .filter((f) => f.status === 'ready' && f.originalAvailable !== false)
        .map((f) => ({
          uploadId: f.uploadId,
          filename: f.filename,
          mimeType: f.mimeType,
          sizeBytes: f.sizeBytes,
          linkedFrom: f.linkedFrom ?? null,
          estimatedTokens: f.estimatedTokens ?? null,
          pages: f.pages ?? null,
        }));
    } catch {
      return;
    }
    const current = this.state.reviewFor(sourceSessionId);
    if (!current || current.draft !== review?.draft) return;
    const recent = recentlyAttachedIds(untracked(this.messageMap.getMessagesForSession(sourceSessionId)));
    const ticked = files.filter((f) => recent.has(f.uploadId)).slice(0, MAX_SHARED_FILES);
    this.state.updateReview(sourceSessionId, { files, fileIds: ticked.map((f) => f.uploadId) });
  }

  private async dismiss(sourceSessionId: string, runId: string): Promise<void> {
    try {
      await firstValueFrom(this.http.post<void>(`${this.runsUrl(sourceSessionId)}/${encodeURIComponent(runId)}/dismiss`, {}));
    } catch {
      // An undismissed run reads as abandoned after a day; nothing for the user to do.
    }
  }

  /** The source's title as the sidenav lists it, for a resumed step's "Context from" link. */
  private sourceTitle(sourceSessionId: string): string {
    const listed = this.sessions.mergedSessionsResource().sessions.find((s) => s.sessionId === sourceSessionId);
    const title = listed?.title?.trim();
    return title && title !== 'New Conversation' ? title : '';
  }

  private runsUrl(sourceSessionId: string): string {
    return `${this.config.appApiUrl()}/sessions/${encodeURIComponent(sourceSessionId)}/workflow-runs`;
  }
}

/** The steps still in the run. */
export function keptSteps(review: RunReview): RunReviewStep[] {
  return review.steps.filter((step) => !step.removed);
}

/** A step's request as it will be sent: its own, else the shared one. */
export function stepRequest(review: RunReview, step: RunReviewStep): string {
  return (step.ownRequest ?? review.request).trim();
}

/** Start is allowed once the sheet is ready, the brief has settled and every kept step has a request. */
export function canStart(review: RunReview): boolean {
  if (review.phase !== 'ready' || review.brief?.status === 'writing') return false;
  const kept = keptSteps(review);
  return kept.length > 0 && kept.every((step) => stepRequest(review, step).length > 0);
}

/** The run row keeps the card as reviewed (PR-W.4b), so its unstarted steps can start anywhere. */
export function canResume(run: WorkflowRunView): boolean {
  return run.status === 'approved' && !!run.card;
}

/** "4 Agents", "1 Agent". */
export function agentsLabel(count: number): string {
  return `${count} ${count === 1 ? 'Agent' : 'Agents'}`;
}

/** Upload ids attached to the source's last few user messages (the single sheet's default ticks). */
function recentlyAttachedIds(messages: readonly Message[]): Set<string> {
  const users = messages.filter((m) => m.role === 'user').slice(-SHARED_FILES_RECENT_TURNS);
  const ids = new Set<string>();
  for (const message of users) {
    for (const block of message.content ?? []) {
      const id = block.fileAttachment?.uploadId;
      if (id) ids.add(id);
    }
  }
  return ids;
}

function describeError(error: unknown): string {
  if (error instanceof HttpErrorResponse && error.status === 404) {
    return 'One of these Agents isn’t available to you, or the conversation can’t be found.';
  }
  if (error instanceof HttpErrorResponse && error.status === 422) {
    return 'Mention each Agent once, and no more than 8.';
  }
  return 'The context for this handoff couldn’t be prepared. Try again.';
}

function describeApproveError(error: unknown): string {
  if (error instanceof HttpErrorResponse && error.status === 409) {
    return 'These Agents were already started or the handoff was cancelled.';
  }
  return 'The Agents couldn’t be started. Try again.';
}
