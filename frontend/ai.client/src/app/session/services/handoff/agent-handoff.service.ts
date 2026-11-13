import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { DestroyRef, Injectable, effect, inject, untracked } from '@angular/core';
import { Router } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { v4 as uuidv4 } from 'uuid';

import { ConfigService } from '../../../services/config.service';
import { FileUploadService } from '../../../services/file-upload/file-upload.service';
import { FEATURES } from '../../../services/features';
import { ToastService } from '../../../services/toast/toast.service';
import { ChatRequestService } from '../chat/chat-request.service';
import { ChatStateService } from '../chat/chat-state.service';
import { ComposerDraftService } from '../session/composer-draft.service';
import { MessageMapService } from '../session/message-map.service';
import { BindingQueryParams, ConversationBindingService } from '../session/conversation-binding.service';
import { buildHandoffMessage, normalizeContextText, referenceUploadId, wrapHandoffContext } from './handoff-prompt';
import {
  BriefFallbackReason,
  CreateHandoffResponse,
  HandoffBrief,
  HandoffDraft,
  HandoffReview,
  HandoffView,
  FileLine,
  MAX_SHARED_FILES,
  SHARED_FILES_RECENT_TURNS,
  ShareableFile,
} from './handoff.models';
import { HandoffBriefService } from './handoff-brief.service';
import { HandoffNotificationService, answeredInSource } from './handoff-notification.service';
import { HandoffStateService } from './handoff-state.service';

/** How long to keep asking for a finished target's status: the Runtime writes it just after the stream. */
const STATUS_REFRESH_ATTEMPTS = 6;
const STATUS_REFRESH_INTERVAL_MS = 700;

/**
 * Handoff actions (docs/specs/agent-handoffs-and-consults.md §4.2–§4.3, PR-0.3).
 *
 * A mid-thread `@`-mention no longer sends. `open` asks app-api for the context card and the exact
 * prompt, and the review sheet shows them in place. `send` starts the Agent's conversation in the
 * background (`ChatRequestService.submitHandoffRequest`) and adds a handoff row to the source
 * thread; the user never leaves it. `cancel` dismisses the handoff and gives the request back to
 * the composer.
 *
 * While a target streams, its row follows the SPA's live per-session state. When the stream ends,
 * the row's persisted status (written by the Runtime after `done`) is fetched, and a toast says
 * what happened if the user isn't looking at the target. A tab with a running handoff asks before
 * it closes, because closing it stops the handoff (§4.6).
 *
 * Each state change also goes to `HandoffNotificationService` (PR-0.3b): it marks the inbox entry
 * read when the user is looking at the source or the target, and shows a browser notification
 * when the page is hidden. The needs-input toast offers to turn browser notifications on.
 *
 * A row that ends in `needs_input` carries `relayedPrompts` (PR-0.3c), read from the target with
 * the row itself, and `HandoffRelayService` renders them under it.
 *
 * With `features.handoffBrief` (PR-0.3a, §4.5.1), `open` then asks the source conversation's own
 * model for a brief and streams it into the card; the excerpt stays as the fallback. Send waits
 * for the brief to settle, so what the user reviewed is what goes.
 */
@Injectable({ providedIn: 'root' })
export class AgentHandoffService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly state = inject(HandoffStateService);
  private readonly chatRequest = inject(ChatRequestService);
  private readonly chatState = inject(ChatStateService);
  private readonly composerDraft = inject(ComposerDraftService);
  private readonly binding = inject(ConversationBindingService);
  private readonly router = inject(Router);
  private readonly toast = inject(ToastService);
  private readonly features = inject(FEATURES);
  private readonly briefs = inject(HandoffBriefService);
  private readonly notifications = inject(HandoffNotificationService);
  private readonly fileUpload = inject(FileUploadService);
  private readonly messageMap = inject(MessageMapService);

  /** Targets seen streaming, so a falling edge is a finish and not a row that never started. */
  private readonly streamingTargets = new Set<string>();

  /**
   * The needs-input toast each handoff has up, by handoff id. That toast never times out, so it is
   * withdrawn here once its row stops waiting, however it got answered (under the row, in the
   * target, in another tab); otherwise it would offer "Answer" on a prompt that is already gone.
   */
  private readonly waitingToasts = new Map<string, string>();

  constructor() {
    effect(() => {
      for (const row of this.state.allRows()) {
        const target = row.targetSessionId;
        if (!target) continue;
        const loading = this.chatState.isSessionLoading(target);
        untracked(() => this.trackTarget(row, target, loading));
      }
    });

    effect(() => {
      for (const row of this.state.allRows()) {
        // A resumed target streams again before its row is re-read, so streaming counts as answered.
        const resumed = !!row.targetSessionId && this.chatState.isSessionLoading(row.targetSessionId);
        if (row.status !== 'needs_input' || resumed) untracked(() => this.withdrawWaitingToast(row.handoffId));
      }
    });

    if (typeof window !== 'undefined') {
      const guard = (event: BeforeUnloadEvent) => {
        if (!this.hasRunningHandoff()) return;
        event.preventDefault();
        // Older browsers need a returnValue to show the prompt.
        event.returnValue = '';
      };
      window.addEventListener('beforeunload', guard);
      inject(DestroyRef).onDestroy(() => window.removeEventListener('beforeunload', guard));
    }
  }

  /** True while any handoff's target is streaming in this tab. Closing the tab would stop it. */
  hasRunningHandoff(): boolean {
    return this.state.allRows().some(
      (row) => !!row.targetSessionId && this.chatState.isSessionLoading(row.targetSessionId),
    );
  }

  /** Open the review sheet for a mid-thread mention. Nothing is sent and no session is created. */
  async open(sourceSessionId: string, draft: HandoffDraft): Promise<void> {
    const review: HandoffReview = {
      draft,
      phase: 'loading',
      request: draft.request,
      card: '',
      cardRemoved: false,
      fileIds: [],
    };
    this.state.setReview(sourceSessionId, review);
    void this.loadShareableFiles(sourceSessionId);
    try {
      const created = await firstValueFrom(
        this.http.post<CreateHandoffResponse>(this.handoffsUrl(sourceSessionId), {
          kind: 'mention',
          agentId: draft.agentId,
          request: draft.request,
        }),
      );
      if (this.state.reviewFor(sourceSessionId) === undefined) {
        // Cancelled while the card was loading: dismiss what the server just opened.
        void this.dismiss(sourceSessionId, created.handoffId);
        return;
      }
      const brief: HandoffBrief | undefined = this.features.handoffBrief ? { status: 'writing', text: '' } : undefined;
      this.state.updateReview(sourceSessionId, {
        phase: 'ready',
        created,
        request: created.request,
        card: created.excerpt,
        brief,
      });
      if (brief) void this.requestBrief(sourceSessionId, created, draft);
    } catch (error) {
      this.state.updateReview(sourceSessionId, {
        phase: 'error',
        error: describeError(error),
      });
    }
  }

  setRequest(sourceSessionId: string, request: string): void {
    this.state.updateReview(sourceSessionId, { request });
  }

  setCard(sourceSessionId: string, card: string): void {
    this.state.updateReview(sourceSessionId, { card });
  }

  setCardRemoved(sourceSessionId: string, removed: boolean): void {
    this.state.updateReview(sourceSessionId, { cardRemoved: removed });
  }

  /** The full first message the Agent will receive, exactly as the server will build it. */
  previewPrompt(review: HandoffReview): string {
    const created = review.created;
    if (!created) return review.request.trim();
    const lines = this.sharedFileLines(review);
    const block = lines.length
      ? wrapHandoffContext(review.cardRemoved ? '' : review.card, created.sourceTitle, lines)
      : review.cardRemoved
        ? ''
        : normalizeContextText(review.card) === normalizeContextText(created.excerpt)
          ? created.contextBlock
          : wrapHandoffContext(review.card, created.sourceTitle);
    return buildHandoffMessage(block, review.request);
  }

  /** Tick or untick one of the source's files. At most `MAX_SHARED_FILES` go. */
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

  /**
   * The `<file>` lines the target's first message will carry (§4.9), as the server writes them:
   * one per ticked file, named by its reference row's id in the target.
   */
  private sharedFileLines(review: HandoffReview): FileLine[] {
    const handoffId = review.created?.handoffId;
    if (!handoffId || !review.fileIds.length) return [];
    const byId = new Map((review.files ?? []).map((f) => [f.uploadId, f]));
    return review.fileIds.flatMap((id) => {
      const file = byId.get(id);
      if (!file) return [];
      const original = file.linkedFrom?.uploadId ?? file.uploadId;
      return [
        {
          uploadId: referenceUploadId(original, handoffId),
          filename: file.filename,
          mimeType: file.mimeType,
          sizeBytes: file.sizeBytes,
        },
      ];
    });
  }

  /**
   * List the source's files for the sheet's checkboxes, ticking those attached in its last
   * `SHARED_FILES_RECENT_TURNS` user turns (up to `MAX_SHARED_FILES`). A failed listing leaves
   * the sheet without files, as before PR-0.4a.
   */
  private async loadShareableFiles(sourceSessionId: string): Promise<void> {
    let files: ShareableFile[];
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
        }));
    } catch {
      return;
    }
    if (!this.state.reviewFor(sourceSessionId)) return;
    const recent = this.recentlyAttached(sourceSessionId);
    const ticked = files.filter((f) => recent.has(f.uploadId)).slice(0, MAX_SHARED_FILES);
    this.state.updateReview(sourceSessionId, { files, fileIds: ticked.map((f) => f.uploadId) });
  }

  /** Upload ids attached to the source's last few user messages. */
  private recentlyAttached(sourceSessionId: string): Set<string> {
    const messages = untracked(this.messageMap.getMessagesForSession(sourceSessionId));
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

  /** Stop waiting for the brief and keep the excerpt in the card. */
  useExcerpt(sourceSessionId: string): void {
    if (this.state.reviewFor(sourceSessionId)?.brief?.status !== 'writing') return;
    this.settleBriefFallback(sourceSessionId, 'preempted');
    void this.briefs.preempt(sourceSessionId);
  }

  /** Close the sheet without sending: dismiss the handoff and give the request back to the composer. */
  cancel(sourceSessionId: string): void {
    const review = this.state.reviewFor(sourceSessionId);
    if (!review) return;
    this.state.clearReview(sourceSessionId);
    // A brief still writing holds the source's lease: stop it the cancel-only way, never Stop's.
    if (review.brief?.status === 'writing') void this.briefs.preempt(sourceSessionId);
    if (review.created) void this.dismiss(sourceSessionId, review.created.handoffId);
    const text = review.request.trim();
    if (text) this.composerDraft.request(sourceSessionId, text);
  }

  /** Send: start the Agent's conversation in the background and add the handoff row. */
  async send(sourceSessionId: string): Promise<void> {
    const review = this.state.reviewFor(sourceSessionId);
    const created = review?.created;
    if (!review || !created || review.phase !== 'ready') return;
    if (review.brief?.status === 'writing') return;
    const request = review.request.trim();
    if (!request) return;

    const targetSessionId = uuidv4();
    const now = new Date().toISOString();
    this.state.updateReview(sourceSessionId, { phase: 'sending' });

    // The row goes in first, so it is there the moment the sheet closes and its watcher sees the
    // target start streaming.
    this.state.upsertRow({
      handoffId: created.handoffId,
      sessionId: sourceSessionId,
      kind: 'mention',
      status: 'running',
      agentId: review.draft.agentId,
      agentName: review.draft.agentName,
      request,
      sourceMessageIndex: created.sourceMessageIndex,
      excerptTokens: created.excerptTokens,
      turnsCovered: created.turnsCovered,
      createdAt: now,
      updatedAt: now,
      targetSessionId,
    });
    this.state.setSpawnedFrom(targetSessionId, {
      sessionId: sourceSessionId,
      handoffId: created.handoffId,
      sourceTitle: created.sourceTitle,
      agentName: review.draft.agentName,
    });
    this.state.clearReview(sourceSessionId);

    this.toast.info(`Handed off to ${review.draft.agentName}`, undefined, {
      action: { label: 'Open', handler: () => this.openTarget(review.draft.agentId, targetSessionId) },
    });

    this.chatRequest
      .submitHandoffRequest({
        targetSessionId,
        agentId: review.draft.agentId,
        message: request,
        handoffId: created.handoffId,
        contextText: review.cardRemoved ? undefined : review.card,
        sharedFileIds: review.fileIds,
        filesUnticked: Math.max(0, (review.files?.length ?? 0) - review.fileIds.length),
        fileUploadIds: review.draft.fileUploadIds,
        invokedSkillIds: review.draft.invokedSkillIds,
        folders: review.draft.folders,
      })
      .catch((error) => console.error('Handoff send failed:', error));
  }

  /** Navigate to a handoff's target conversation, with its Agent binding on the URL. */
  openTarget(agentId: string, targetSessionId: string): void {
    void this.router.navigate(['/s', targetSessionId], {
      queryParams: this.binding.queryParamsFor(agentId),
    });
  }

  /** The URL the handoff row links to, with its Agent binding. */
  targetLink(row: HandoffView): { commands: string[]; queryParams: BindingQueryParams } | null {
    if (!row.targetSessionId) return null;
    return {
      commands: ['/s', row.targetSessionId],
      queryParams: this.binding.queryParamsFor(row.agentId),
    };
  }

  /**
   * Ask the source's model for the brief and stream it into the card (§4.5.1). Every frame is
   * checked against the review it was started for, so a brief that outlives its sheet (cancelled,
   * sent, replaced by another mention) changes nothing.
   */
  private async requestBrief(sourceSessionId: string, created: CreateHandoffResponse, draft: HandoffDraft): Promise<void> {
    const handoffId = created.handoffId;
    const writing = (): HandoffReview | undefined => {
      const review = this.state.reviewFor(sourceSessionId);
      return review?.created?.handoffId === handoffId && review.brief?.status === 'writing' ? review : undefined;
    };

    let body: Record<string, unknown>;
    try {
      // A brief from an earlier sheet on this thread would hold the lease and answer `busy`.
      await this.briefs.preempt(sourceSessionId);
      body = await this.chatRequest.buildHandoffBriefRequest({
        sourceSessionId,
        handoffId,
        request: created.request,
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

  private trackTarget(row: HandoffView, target: string, loading: boolean): void {
    if (loading) {
      this.streamingTargets.add(target);
      if (row.status !== 'running') this.state.upsertRow({ ...row, status: 'running' });
      return;
    }
    if (!this.streamingTargets.delete(target)) return;
    void this.refreshAfterTurn(row);
  }

  /**
   * The target's turn ended: read the status the Runtime wrote, then say so if the user isn't there.
   *
   * A row the server still has `open` was never taken by the Runtime: the turn ended before its
   * stream began, refused (quota, an unavailable Agent) or never delivered (a 502, a 409). The
   * target says which, and a delivery failure offers "Not sent" with a retry that re-sends the same
   * handoff. Until then the row reads stopped rather than running forever.
   */
  private async refreshAfterTurn(row: HandoffView): Promise<void> {
    let last: HandoffView | undefined;
    for (let attempt = 0; attempt < STATUS_REFRESH_ATTEMPTS; attempt++) {
      await delay(STATUS_REFRESH_INTERVAL_MS);
      try {
        last = await firstValueFrom(
          this.http.get<HandoffView>(`${this.handoffsUrl(row.sessionId)}/${encodeURIComponent(row.handoffId)}`),
        );
      } catch {
        continue;
      }
      if (this.chatState.isSessionLoading(row.targetSessionId ?? '')) return;
      if (last.status === 'running') continue;
      if (last.status === 'open') break;
      this.state.upsertRow(last);
      this.announce(last);
      return;
    }
    if (this.chatState.isSessionLoading(row.targetSessionId ?? '')) return;
    const stopped: HandoffView = { ...(last ?? row), status: 'stopped', stoppedReason: 'not_started' };
    this.state.upsertRow({ ...stopped, targetSessionId: row.targetSessionId });
    this.announce(stopped);
  }

  /**
   * Say what changed, unless the user is looking at the target. A prompt the source relays gets
   * "Answer", which opens the source at the row (PR-0.3c); a browser sign-in, and every other
   * state, gets "Open" for the target.
   */
  private announce(row: HandoffView): void {
    this.notifications.onStateChange(row);
    // A new state replaces whatever this handoff was waiting on, including a second pause's toast.
    this.withdrawWaitingToast(row.handoffId);
    const target = row.targetSessionId;
    if (!target || this.chatState.viewedSessionId() === target) return;
    const action = answeredInSource(row)
      ? { label: 'Answer', handler: () => this.notifications.openSource({ handoffId: row.handoffId, sourceSessionId: row.sessionId }) }
      : { label: 'Open', handler: () => this.openTarget(row.agentId, target) };
    if (row.status === 'done') {
      this.toast.success(`${row.agentName} finished`, undefined, { action });
    } else if (row.status === 'needs_input') {
      // The first time something waits on the user is when a browser notification makes sense.
      const secondaryAction = this.notifications.canOffer()
        ? { label: 'Get a browser notification next time', handler: () => void this.notifications.requestPermission(row) }
        : undefined;
      const toastId = this.toast.warning(`${row.agentName} needs your input`, undefined, {
        action,
        secondaryAction,
        duration: 0,
      });
      this.waitingToasts.set(row.handoffId, toastId);
    } else if (row.status === 'stopped') {
      this.toast.info(`${row.agentName} stopped`, undefined, { action });
    }
  }

  private withdrawWaitingToast(handoffId: string): void {
    const toastId = this.waitingToasts.get(handoffId);
    if (toastId === undefined) return;
    this.waitingToasts.delete(handoffId);
    this.toast.dismiss(toastId);
  }

  private async dismiss(sourceSessionId: string, handoffId: string): Promise<void> {
    try {
      await firstValueFrom(
        this.http.post<void>(`${this.handoffsUrl(sourceSessionId)}/${encodeURIComponent(handoffId)}/dismiss`, {}),
      );
    } catch {
      // An undismissed handoff reads as abandoned after a day; nothing for the user to do.
    }
  }

  private handoffsUrl(sourceSessionId: string): string {
    return `${this.config.appApiUrl()}/sessions/${encodeURIComponent(sourceSessionId)}/handoffs`;
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function describeError(error: unknown): string {
  if (error instanceof HttpErrorResponse && error.status === 404) {
    return 'This Agent isn’t available to you, or the conversation can’t be found.';
  }
  return 'The context for this handoff couldn’t be prepared. Try again.';
}
