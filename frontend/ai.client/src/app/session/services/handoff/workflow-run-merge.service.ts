import { Injectable, inject, signal } from '@angular/core';

import { ToastService } from '../../../services/toast/toast.service';
import { ChatStateService } from '../chat/chat-state.service';
import { ComposerEmptyService } from '../session/composer-empty.service';
import { DeliveryStateService } from './delivery-state.service';
import { WorkflowRunStateService } from './workflow-run-state.service';
import { WorkflowRunView } from './workflow-run.models';

/**
 * Sends one turn in the conversation on screen, the way its composer's Send would: the page's own
 * binding, model and selections. The cards waiting on the composer ride it (`takeForSend`).
 */
export type MergeSender = (text: string) => Promise<void>;

/** Why a merge didn't start by itself; each leaves the card waiting on the composer. */
export type MergeHold =
  | 'not_merge'
  | 'not_on_screen'
  | 'busy'
  | 'composer_not_empty'
  | 'card_not_waiting'
  | 'other_cards'
  | 'sheet_open';

/** The run's merge instruction, or null when its result is a card (PR-D.5b). */
export function mergeInstructionOf(run: Pick<WorkflowRunView, 'workflow'> | null | undefined): string | null {
  const source = run?.workflow;
  if (source?.resultMode !== 'merge') return null;
  return source.resultInstruction?.trim() || null;
}

/**
 * The merge that starts by itself (docs/specs/workflow-designer.md §20.1, workflow-runs.md §7,
 * PR-D.5b): when a saved workflow's run whose Result merges has settled, ONE turn in the source
 * conversation, whose text is the merge instruction and which carries the run's result card through
 * the ordinary `deliveries` path. The source conversation's model writes it (§17, question 6).
 *
 * - **Exactly once.** Only the tab whose collect made the card (`created: true`, which is the run's
 *   `claim_result` on the server) ever asks; a second tab, a reload and a later collect get
 *   `created: false` and never do. Nothing else is stored to say a merge happened: the card itself
 *   is attached by that one send, as any card is.
 * - **Only when nothing is in the way, checked at the moment of sending:** the source is the
 *   conversation on screen (its page registered a sender), it is not streaming, its composer is
 *   empty (`ComposerEmptyService`), the run's card is on the composer and is the only card there,
 *   and no run sheet is open on it. Otherwise the card waits on the composer, as in step 1, and the
 *   run's row offers Merge now.
 * - **Why on screen.** A send is built from the page's selections, and the session page restores a
 *   conversation's last model when it is opened. Sent from elsewhere, the merge would run on another
 *   conversation's model and binding, and a model switch re-writes the source's cached prefix.
 */
@Injectable({ providedIn: 'root' })
export class WorkflowRunMergeService {
  private readonly chatState = inject(ChatStateService);
  private readonly composer = inject(ComposerEmptyService);
  private readonly deliveryState = inject(DeliveryStateService);
  private readonly runState = inject(WorkflowRunStateService);
  private readonly toast = inject(ToastService);

  private readonly senders = signal<ReadonlyMap<string, MergeSender>>(new Map());
  private readonly sending = signal<ReadonlySet<string>>(new Set());

  /**
   * The session page on screen offers its send for `sessionId`. Returns the unregister, which
   * removes the sender only if it is still this one.
   */
  register(sessionId: string, sender: MergeSender): () => void {
    this.senders.update((map) => new Map(map).set(sessionId, sender));
    return () =>
      this.senders.update((map) => {
        if (map.get(sessionId) !== sender) return map;
        const next = new Map(map);
        next.delete(sessionId);
        return next;
      });
  }

  /** A merge send for this run is in flight. */
  isSending(runId: string): boolean {
    return this.sending().has(runId);
  }

  /**
   * What holds the merge back right now, or null when it can start. Reactive: the run's row reads
   * it for Merge now. `deliveryId` is the run's result card.
   */
  holdFor(run: WorkflowRunView, deliveryId: string | null | undefined, options: { manual?: boolean } = {}): MergeHold | null {
    if (mergeInstructionOf(run) === null) return 'not_merge';
    const sessionId = run.sessionId;
    if (!this.senders().has(sessionId)) return 'not_on_screen';
    if (this.chatState.isSessionLoading(sessionId) || this.isSending(run.runId)) return 'busy';
    if (this.runState.reviewFor(sessionId)) return 'sheet_open';
    const cards = this.deliveryState.cardsFor(sessionId);
    if (!deliveryId || !cards.some((card) => card.deliveryId === deliveryId)) return 'card_not_waiting';
    // Merge now is the user's own press: their draft stays in the composer, and any other card
    // waiting there rides the send, as it would ride their own.
    if (options.manual) return null;
    if (cards.length > 1) return 'other_cards';
    if (!this.composer.isEmpty(sessionId)) return 'composer_not_empty';
    return null;
  }

  /**
   * Start the merge by itself, right after this tab's collect made the run's card. Re-checks every
   * condition now, not when the run settled, so a user who began typing in between keeps the card.
   * True when the turn was sent; it streams on in the background.
   */
  startIfReady(run: WorkflowRunView, deliveryId: string): boolean {
    if (this.holdFor(run, deliveryId) !== null) return false;
    return this.send(run, 'auto');
  }

  /** Merge now, from the run's row: the user asked, so a draft or another card doesn't hold it. */
  startNow(run: WorkflowRunView, deliveryId: string | null | undefined): boolean {
    if (this.holdFor(run, deliveryId, { manual: true }) !== null) return false;
    return this.send(run, 'manual');
  }

  private send(run: WorkflowRunView, how: 'auto' | 'manual'): boolean {
    const instruction = mergeInstructionOf(run);
    const sender = this.senders().get(run.sessionId);
    if (!instruction || !sender) return false;
    this.sending.update((set) => new Set(set).add(run.runId));
    if (how === 'auto') this.toast.info('Merging the results in this conversation');
    // The send takes the composer's cards before its first await, so the card is spoken for now.
    // A refused send is marked "Not sent" with a retry, and the card goes back, by the send path.
    sender(instruction)
      .catch(() => undefined)
      .finally(() =>
        this.sending.update((set) => {
          const next = new Set(set);
          next.delete(run.runId);
          return next;
        }),
      );
    return true;
  }
}
