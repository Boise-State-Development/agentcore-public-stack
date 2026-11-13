import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { firstValueFrom } from 'rxjs';

import { ConfigService } from '../../../services/config.service';
import { ToastService } from '../../../services/toast/toast.service';
import { DeliveryStateService } from './delivery-state.service';
import {
  CreateSendBackResponse,
  DeliverSendBackResponse,
  DeliveryView,
  HandoffView,
} from './handoff.models';

/**
 * Sending a handoff's result back (handoffs spec §4.3) as a delivery (§4.8), PR-0.4.
 *
 * Two ways in, one card out. **Bring back result**, on a finished handoff row in the source, makes
 * the card at once from the target's last answer. **Send to *{title}***, on an answer in the target,
 * opens a review sheet first, and Send makes the card from what the user kept. Neither navigates:
 * the card waits on the source's composer, as a row, on any device, until the user sends there.
 *
 * Removing a card dismisses it on the server, so it doesn't come back on the next load.
 */
@Injectable({ providedIn: 'root' })
export class DeliveryService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly state = inject(DeliveryStateService);
  private readonly toast = inject(ToastService);
  private readonly router = inject(Router);

  private readonly bringing = signal<ReadonlySet<string>>(new Set());
  private readonly broughtBack = signal<ReadonlySet<string>>(new Set());

  /** A Bring back result for this handoff is in flight. */
  isBringingBack(handoffId: string): boolean {
    return this.bringing().has(handoffId);
  }

  /** This handoff's result was put on the composer in this tab. */
  wasBroughtBack(handoffId: string): boolean {
    return this.broughtBack().has(handoffId);
  }

  /** Bring back result: the target's last answer becomes a card on this (the source's) composer. */
  async bringBack(row: HandoffView): Promise<void> {
    const target = row.targetSessionId;
    if (!target || this.isBringingBack(row.handoffId)) return;
    this.bringing.update((s) => new Set(s).add(row.handoffId));
    try {
      const created = await firstValueFrom(
        this.http.post<CreateSendBackResponse>(`${this.sessionsUrl(target)}/handoffs/send-back`, { deliver: true }),
      );
      if (!created.deliveryId) return;
      this.state.add(row.sessionId, toCard(created, target, created.deliveryId, created.excerpt, created.artifacts));
      this.broughtBack.update((s) => new Set(s).add(row.handoffId));
      this.toast.success('Result added to your next message');
    } catch (error) {
      this.toast.error('The result couldn’t be brought back', describe(error));
    } finally {
      this.bringing.update((s) => {
        const next = new Set(s);
        next.delete(row.handoffId);
        return next;
      });
    }
  }

  /** Remove a card from a composer. It is dismissed on the server, so a reload doesn't bring it back. */
  async dismissCard(sessionId: string, card: DeliveryView): Promise<void> {
    this.state.remove(sessionId, card.deliveryId);
    try {
      await firstValueFrom(
        this.http.post<void>(`${this.sessionsUrl(sessionId)}/deliveries/${encodeURIComponent(card.deliveryId)}/dismiss`, {}),
      );
    } catch (error) {
      // 409: a send already carried it, so there is nothing to put back. 404: it's already gone.
      if (error instanceof HttpErrorResponse && (error.status === 409 || error.status === 404)) return;
      this.state.restore(sessionId, [card]);
      this.toast.error('The card couldn’t be removed', 'Try again.');
    }
  }

  /** Send to *{title}*: open the review for one of this (the target's) answers. Nothing is sent yet. */
  async openSendBack(targetSessionId: string, messageIndex: number): Promise<void> {
    const open = this.state.reviewFor(targetSessionId);
    if (open && open.phase !== 'error') return;
    this.state.setReview(targetSessionId, { messageIndex, phase: 'loading', text: '', artifactIds: [] });
    try {
      const created = await firstValueFrom(
        this.http.post<CreateSendBackResponse>(`${this.sessionsUrl(targetSessionId)}/handoffs/send-back`, { messageIndex }),
      );
      if (this.state.reviewFor(targetSessionId)?.messageIndex !== messageIndex) {
        // Cancelled while loading: close what the server just opened.
        void this.dismissSendBack(targetSessionId, created.handoffId);
        return;
      }
      this.state.updateReview(targetSessionId, {
        phase: 'ready',
        created,
        text: created.excerpt,
        artifactIds: created.artifacts.map((a) => a.artifactId),
      });
    } catch (error) {
      this.state.updateReview(targetSessionId, { phase: 'error', error: describe(error) });
    }
  }

  setSendBackText(targetSessionId: string, text: string): void {
    this.state.updateReview(targetSessionId, { text });
  }

  toggleSendBackArtifact(targetSessionId: string, artifactId: string, included: boolean): void {
    const review = this.state.reviewFor(targetSessionId);
    if (!review) return;
    const rest = review.artifactIds.filter((id) => id !== artifactId);
    this.state.updateReview(targetSessionId, { artifactIds: included ? [...rest, artifactId] : rest });
  }

  /** Make the card from the review. The user stays in the target; a toast offers the source. */
  async sendBack(targetSessionId: string): Promise<void> {
    const review = this.state.reviewFor(targetSessionId);
    const created = review?.created;
    if (!review || !created || review.phase !== 'ready') return;
    this.state.updateReview(targetSessionId, { phase: 'sending' });
    try {
      const sent = await firstValueFrom(
        this.http.post<DeliverSendBackResponse>(
          `${this.sessionsUrl(targetSessionId)}/handoffs/${encodeURIComponent(created.handoffId)}/deliver`,
          { text: review.text, artifactIds: review.artifactIds },
        ),
      );
      this.state.clearReview(targetSessionId);
      const artifacts = created.artifacts.filter((a) => review.artifactIds.includes(a.artifactId));
      this.state.add(sent.sourceSessionId, toCard(created, targetSessionId, sent.deliveryId, review.text, artifacts));
      this.toast.success(`Sent to ${sent.sourceTitle || 'the original conversation'}`, undefined, {
        action: { label: 'Open', handler: () => void this.router.navigate(['/s', sent.sourceSessionId]) },
      });
    } catch (error) {
      if (error instanceof HttpErrorResponse && error.status === 409) {
        this.state.clearReview(targetSessionId);
        this.toast.info('This result was already sent');
        return;
      }
      this.state.updateReview(targetSessionId, { phase: 'ready', error: describe(error) });
    }
  }

  /** Close the review without sending. */
  cancelSendBack(targetSessionId: string): void {
    const review = this.state.reviewFor(targetSessionId);
    this.state.clearReview(targetSessionId);
    if (review?.created) void this.dismissSendBack(targetSessionId, review.created.handoffId);
  }

  private async dismissSendBack(targetSessionId: string, handoffId: string): Promise<void> {
    try {
      await firstValueFrom(
        this.http.post<void>(`${this.sessionsUrl(targetSessionId)}/handoffs/${encodeURIComponent(handoffId)}/dismiss`, {}),
      );
    } catch {
      // An undismissed send-back reads as abandoned after a day; nothing for the user to do.
    }
  }

  private sessionsUrl(sessionId: string): string {
    return `${this.config.appApiUrl()}/sessions/${encodeURIComponent(sessionId)}`;
  }
}

function toCard(
  created: CreateSendBackResponse,
  fromSessionId: string,
  deliveryId: string,
  text: string,
  artifacts: DeliveryView['artifacts'],
): DeliveryView {
  return {
    deliveryId,
    fromSessionId,
    fromTitle: created.fromTitle,
    handoffId: created.handoffId,
    origin: 'user',
    text,
    artifacts,
    createdAt: new Date().toISOString(),
  };
}

function describe(error: unknown): string {
  if (error instanceof HttpErrorResponse) {
    const detail = typeof error.error?.detail === 'string' ? error.error.detail : undefined;
    if (error.status === 404) return 'The conversation or answer can’t be found.';
    if (detail) return detail;
  }
  return 'Something went wrong. Try again.';
}
