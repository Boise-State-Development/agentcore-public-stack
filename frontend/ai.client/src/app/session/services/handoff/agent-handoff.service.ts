import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { DestroyRef, Injectable, effect, inject, untracked } from '@angular/core';
import { Router } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { v4 as uuidv4 } from 'uuid';

import { ConfigService } from '../../../services/config.service';
import { ToastService } from '../../../services/toast/toast.service';
import { ChatRequestService } from '../chat/chat-request.service';
import { ChatStateService } from '../chat/chat-state.service';
import { ComposerDraftService } from '../session/composer-draft.service';
import { BindingQueryParams, ConversationBindingService } from '../session/conversation-binding.service';
import { buildHandoffMessage, normalizeContextText, wrapHandoffContext } from './handoff-prompt';
import { CreateHandoffResponse, HandoffDraft, HandoffReview, HandoffView } from './handoff.models';
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

  /** Targets seen streaming, so a falling edge is a finish and not a row that never started. */
  private readonly streamingTargets = new Set<string>();

  constructor() {
    effect(() => {
      for (const row of this.state.allRows()) {
        const target = row.targetSessionId;
        if (!target) continue;
        const loading = this.chatState.isSessionLoading(target);
        untracked(() => this.trackTarget(row, target, loading));
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
    };
    this.state.setReview(sourceSessionId, review);
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
      this.state.updateReview(sourceSessionId, {
        phase: 'ready',
        created,
        request: created.request,
        card: created.excerpt,
      });
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
    const block = review.cardRemoved
      ? ''
      : normalizeContextText(review.card) === normalizeContextText(created.excerpt)
        ? created.contextBlock
        : wrapHandoffContext(review.card, created.sourceTitle);
    return buildHandoffMessage(block, review.request);
  }

  /** Close the sheet without sending: dismiss the handoff and give the request back to the composer. */
  cancel(sourceSessionId: string): void {
    const review = this.state.reviewFor(sourceSessionId);
    if (!review) return;
    this.state.clearReview(sourceSessionId);
    if (review.created) void this.dismiss(sourceSessionId, review.created.handoffId);
    const text = review.request.trim();
    if (text) this.composerDraft.request(sourceSessionId, text);
  }

  /** Send: start the Agent's conversation in the background and add the handoff row. */
  async send(sourceSessionId: string): Promise<void> {
    const review = this.state.reviewFor(sourceSessionId);
    const created = review?.created;
    if (!review || !created || review.phase !== 'ready') return;
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

  private announce(row: HandoffView): void {
    const target = row.targetSessionId;
    if (!target || this.chatState.viewedSessionId() === target) return;
    const action = { label: 'Open', handler: () => this.openTarget(row.agentId, target) };
    if (row.status === 'done') {
      this.toast.success(`${row.agentName} finished`, undefined, { action });
    } else if (row.status === 'needs_input') {
      this.toast.warning(`${row.agentName} needs your input`, undefined, { action, duration: 0 });
    } else if (row.status === 'stopped') {
      this.toast.info(`${row.agentName} stopped`, undefined, { action });
    }
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
