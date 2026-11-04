import { HttpClient, HttpContext } from '@angular/common/http';
import { Injectable, effect, inject, signal, untracked } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { SUPPRESS_ERROR_TOAST } from '../../../auth/error.interceptor';
import { ConfigService } from '../../../services/config.service';
import { FEATURES } from '../../../services/features';
import { OAuthConsentRequest, OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import { ToastService } from '../../../services/toast/toast.service';
import { ToolApprovalRequest, ToolApprovalService } from '../../../services/tool-approval/tool-approval.service';
import { UserQuestionRequest, UserQuestionService } from '../../../services/user-question/user-question.service';
import { validateUserQuestions } from '../../../shared/utils/stream-parser';
import { ChatStateService } from '../chat/chat-state.service';
import { MessageMapService } from '../session/message-map.service';
import { HandoffView, RelayedPrompt, RelayedPromptKind } from './handoff.models';
import { HandoffStateService } from './handoff-state.service';

/**
 * How a resume of a paused turn ended.
 *
 * - `resumed`: the turn picked up the answer.
 * - `conflict`: the answer arrived after the pause was already answered or gone: the resume guard
 *   refused it (`missing`, `unknown_interrupt`) or the turn was already running again (409).
 * - `expired`: the pause outlived its snapshot.
 * - `failed`: anything else; the caller's own error handling applies.
 */
export type ResumeOutcome = 'resumed' | 'conflict' | 'expired' | 'failed';

/** Where a handoff's prompt was answered (§4.7 `RelayedPromptAnswered`). */
export type RelaySurface = 'source' | 'target';

/** A resume of a handoff target in flight, from `beginResume` to `endResume`. */
export interface RelayedResume {
  targetSessionId: string;
  kind: RelayedPromptKind;
  surface: RelaySurface;
  sourceSessionId: string;
  handoffId: string;
  agentName: string;
}

/** The one line a relay collapses to once it was answered (or turned out to be). */
export interface RelayNotice {
  text: string;
  tone: 'success' | 'info';
}

type RelayEvent =
  | { metric: 'RelayedPromptShown' | 'RelayedPromptConflict'; interruptKind: RelayedPromptKind }
  | { metric: 'RelayedPromptAnswered'; interruptKind: RelayedPromptKind; surface: RelaySurface };

/**
 * Relayed prompts (handoffs spec §4.3 "Relayed prompts", PR-0.3c).
 *
 * A handoff runs in the background so the user can stay in the source. When its target pauses on
 * the user, the source relays that prompt under the handoff row, labelled as the target's, and the
 * answer resumes the **target**: the ordinary resume request, on the target's session, with its
 * `interruptId`. Nothing navigates, and nothing reaches the source's model.
 *
 * The prompts come from `relayedPrompts` on the row (the target's breadcrumbs, read by app-api on
 * `GET /messages` and on the row read when a target's turn ends). This service puts them in the
 * same interrupt services the target's own thread reads, tagged with the **target's** session id:
 *
 * - the source's message list filters every prompt slot by its own session, so a target's prompt
 *   can never render there as though the source were waiting;
 * - answering in either place resolves the one request, so the other surface clears at once;
 * - the answer goes through `ChatRequestService`'s resume, which is session-addressed and keeps
 *   `viewedSessionId` and the route where they are.
 *
 * Registration re-runs when the consent queue changes, because `session.page` clears it on every
 * route change (the gap PR-0.3 noted): a relay outlives that clear and puts its prompt back. It
 * expires with its interrupt: once a row stops waiting (a fresh turn in the target superseded the
 * pause, or the row was re-read without it), what this service registered for it is withdrawn,
 * so nothing is left to answer for an interrupt that no longer exists.
 *
 * `ChatRequestService` reports every resume of a handoff target here (`beginResume` /
 * `endResume`), which is how first-answer-wins reads: the resume guard's refusal, or a 409 from
 * the turn the other surface already resumed, becomes "Already answered" and a refresh, never an
 * error.
 */
@Injectable({ providedIn: 'root' })
export class HandoffRelayService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly features = inject(FEATURES);
  private readonly state = inject(HandoffStateService);
  private readonly chatState = inject(ChatStateService);
  private readonly messageMap = inject(MessageMapService);
  private readonly consent = inject(OAuthConsentService);
  private readonly approvals = inject(ToolApprovalService);
  private readonly questions = inject(UserQuestionService);
  private readonly toast = inject(ToastService);

  private readonly options = { context: new HttpContext().set(SUPPRESS_ERROR_TOAST, true) };

  private readonly notices = signal<ReadonlyMap<string, RelayNotice>>(new Map());
  /** `handoffId:interruptId` pairs already counted as shown. */
  private readonly shown = new Set<string>();
  /** Targets with a resume this service is following, so a 409 there stays quiet. */
  private readonly resuming = new Set<string>();
  /** What `register` put in the interrupt services, by handoff, so it can be withdrawn. */
  private readonly registered = new Map<string, { target: string; prompts: RelayedPrompt[] }>();

  constructor() {
    if (!this.features.handoffs) return;
    effect(() => {
      const rows = this.state.allRows();
      // Re-run after `OAuthConsentService.clear()` empties the queue on a route change.
      this.consent.pending();
      untracked(() => {
        for (const row of rows) this.register(row);
      });
    });
  }

  /** The prompts a row relays right now: waiting, and the source not mid-turn. */
  isWaiting(row: HandoffView): boolean {
    const target = row.targetSessionId;
    return (
      row.status === 'needs_input' &&
      !!target &&
      !!row.relayedPrompts?.length &&
      !this.chatState.isSessionLoading(target)
    );
  }

  /** The live consent request a relayed consent answers through, if it is still pending. */
  consentFor(row: HandoffView, prompt: RelayedPrompt): OAuthConsentRequest | undefined {
    return this.consent
      .pending()
      .find((r) => r.providerId === prompt.providerId && r.sessionId === row.targetSessionId);
  }

  approvalFor(row: HandoffView, prompt: RelayedPrompt): ToolApprovalRequest | undefined {
    return this.approvals
      .pending()
      .find((r) => r.interruptId === prompt.interruptId && r.sessionId === row.targetSessionId);
  }

  questionFor(row: HandoffView, prompt: RelayedPrompt): UserQuestionRequest | undefined {
    return this.questions
      .pending()
      .find((r) => r.interruptId === prompt.interruptId && r.sessionId === row.targetSessionId);
  }

  notice(handoffId: string): RelayNotice | undefined {
    return this.notices().get(handoffId);
  }

  clearNotice(handoffId: string): void {
    this.notices.update((map) => {
      if (!map.has(handoffId)) return map;
      const next = new Map(map);
      next.delete(handoffId);
      return next;
    });
  }

  /** Count a relayed prompt the user could see, once per interrupt. */
  markShown(row: HandoffView, prompt: RelayedPrompt): void {
    const key = `${row.handoffId}:${prompt.interruptId}`;
    if (this.shown.has(key)) return;
    this.shown.add(key);
    this.record(row.sessionId, row.handoffId, { metric: 'RelayedPromptShown', interruptKind: prompt.kind });
  }

  /** Whether a 409 on this session's resume is ours to explain. */
  isResuming(sessionId: string): boolean {
    return this.resuming.has(sessionId);
  }

  /**
   * A paused turn on `sessionId` is about to resume. Returns a handle when that session is a
   * handoff target waiting on the user, else null (an ordinary resume, nothing to relay).
   *
   * The surface is where the user is: answering while looking at the target is `target`; anything
   * else (the relay, or a consent popup that completes after the user moved on) is `source`. An
   * answer in the target is counted now, before the turn moves the row, and app-api counts it only
   * while the handoff is still `needs_input`.
   */
  beginResume(sessionId: string, kind: RelayedPromptKind): RelayedResume | null {
    if (!this.features.handoffs) return null;
    const surface: RelaySurface = this.chatState.viewedSessionId() === sessionId ? 'target' : 'source';
    const row = this.state.allRows().find((r) => r.targetSessionId === sessionId);
    let resume: RelayedResume;
    if (row) {
      if (row.status !== 'needs_input') return null;
      resume = {
        targetSessionId: sessionId,
        kind,
        surface,
        sourceSessionId: row.sessionId,
        handoffId: row.handoffId,
        agentName: row.agentName,
      };
    } else {
      // This tab never loaded the source: the target knows where it came from, not the row.
      const spawned = this.state.spawnedFrom(sessionId);
      if (!spawned || surface !== 'target') return null;
      resume = {
        targetSessionId: sessionId,
        kind,
        surface,
        sourceSessionId: spawned.sessionId,
        handoffId: spawned.handoffId,
        agentName: spawned.agentName,
      };
    }
    this.resuming.add(sessionId);
    if (surface === 'target') {
      this.record(resume.sourceSessionId, resume.handoffId, {
        metric: 'RelayedPromptAnswered',
        interruptKind: kind,
        surface,
      });
    }
    return resume;
  }

  /** The resume `beginResume` started has ended. */
  endResume(resume: RelayedResume, outcome: ResumeOutcome): void {
    this.resuming.delete(resume.targetSessionId);
    const { sourceSessionId, handoffId, agentName, kind, surface } = resume;
    switch (outcome) {
      case 'resumed': {
        if (surface !== 'source') return;
        this.record(sourceSessionId, handoffId, { metric: 'RelayedPromptAnswered', interruptKind: kind, surface });
        this.setNotice(handoffId, {
          text: `${kind === 'oauth' ? 'Connected' : 'Answered'} · ${agentName} resumed`,
          tone: 'success',
        });
        const row = this.state.rowsFor(sourceSessionId).find((r) => r.handoffId === handoffId);
        if (row?.relayedPrompts) this.state.upsertRow({ ...row, relayedPrompts: undefined });
        return;
      }
      case 'conflict':
        this.record(sourceSessionId, handoffId, { metric: 'RelayedPromptConflict', interruptKind: kind });
        if (surface === 'source') {
          this.setNotice(handoffId, { text: 'Already answered', tone: 'info' });
        } else {
          this.toast.info('Already answered', `${agentName} already has your answer.`);
        }
        void this.refresh(resume);
        return;
      case 'expired':
        if (surface !== 'source') return;
        this.setNotice(handoffId, {
          text: `${agentName} can no longer pick up where it left off. Open the conversation to send it again.`,
          tone: 'info',
        });
        void this.refresh(resume);
        return;
      default:
        return;
    }
  }

  /**
   * After a lost race: re-read the target, so its thread shows the answer that won rather than the
   * refusal this resume streamed (which is never persisted), and re-read the row, so the relay and
   * the status line say what is true now.
   */
  private async refresh(resume: RelayedResume): Promise<void> {
    void this.messageMap.reloadMessagesForSession(resume.targetSessionId);
    const row = this.state.rowsFor(resume.sourceSessionId).find((r) => r.handoffId === resume.handoffId);
    if (!row) return;
    try {
      const fresh = await firstValueFrom(
        this.http.get<HandoffView>(this.handoffUrl(resume.sourceSessionId, resume.handoffId), this.options),
      );
      this.state.upsertRow({ ...fresh, targetSessionId: fresh.targetSessionId ?? row.targetSessionId });
    } catch {
      // The row keeps its last status; the next turn's refresh or a reload corrects it.
    }
  }

  /** Put a waiting row's prompts where the target's own thread would put them, once. */
  private register(row: HandoffView): void {
    const target = row.targetSessionId;
    const prompts = row.status === 'needs_input' && target ? (row.relayedPrompts ?? []) : [];
    this.withdrawStale(row.handoffId, prompts);
    if (!target || prompts.length === 0) return;
    this.registered.set(row.handoffId, { target, prompts });
    for (const prompt of prompts) {
      switch (prompt.kind) {
        case 'oauth': {
          if (!prompt.providerId) break;
          // The queue is keyed by provider. Never take over another conversation's request for it:
          // one connection answers both, and that conversation's prompt resumes its own turn.
          if (this.consent.pending().some((r) => r.providerId === prompt.providerId)) break;
          this.consent.requestConsent(prompt.providerId, undefined, prompt.interruptId, undefined, target);
          break;
        }
        case 'tool_approval':
          if (!prompt.toolName) break;
          this.approvals.requestApproval({
            interruptId: prompt.interruptId,
            toolUseId: prompt.toolUseId ?? '',
            toolName: prompt.toolName,
            toolInput: prompt.toolInput,
            message: prompt.message ?? '',
            sessionId: target,
          });
          break;
        case 'user_question': {
          let parsed: unknown;
          try {
            parsed = JSON.parse(prompt.questions ?? '');
          } catch {
            break;
          }
          if (!validateUserQuestions(parsed)) break;
          this.questions.requestAnswers({
            interruptId: prompt.interruptId,
            toolUseId: prompt.toolUseId ?? '',
            questions: parsed,
            sessionId: target,
          });
          break;
        }
        default:
          // A browser sign-in is answered in the target, where its live view is: nothing to register.
          break;
      }
    }
  }

  /** Take back what was registered for a handoff and is no longer among its relayed prompts. */
  private withdrawStale(handoffId: string, current: RelayedPrompt[]): void {
    const previous = this.registered.get(handoffId);
    if (!previous) return;
    const live = new Set(current.map((p) => p.interruptId));
    for (const prompt of previous.prompts) {
      if (live.has(prompt.interruptId)) continue;
      switch (prompt.kind) {
        case 'oauth': {
          const request = this.consent.pending().find((r) => r.providerId === prompt.providerId);
          if (request?.sessionId === previous.target && request.interruptId === prompt.interruptId) {
            this.consent.dismiss(prompt.providerId ?? '', { syncServer: false });
          }
          break;
        }
        case 'tool_approval':
          this.approvals.withdraw(prompt.interruptId);
          break;
        case 'user_question':
          this.questions.withdraw(prompt.interruptId);
          break;
        default:
          break;
      }
    }
    if (current.length === 0) this.registered.delete(handoffId);
  }

  private setNotice(handoffId: string, notice: RelayNotice): void {
    this.notices.update((map) => new Map(map).set(handoffId, notice));
  }

  private record(sourceSessionId: string, handoffId: string, event: RelayEvent): void {
    this.http
      .post<void>(`${this.handoffUrl(sourceSessionId, handoffId)}/notifications/events`, event, this.options)
      .subscribe({ error: () => undefined });
  }

  private handoffUrl(sourceSessionId: string, handoffId: string): string {
    return `${this.config.appApiUrl()}/sessions/${encodeURIComponent(sourceSessionId)}/handoffs/${encodeURIComponent(handoffId)}`;
  }
}
