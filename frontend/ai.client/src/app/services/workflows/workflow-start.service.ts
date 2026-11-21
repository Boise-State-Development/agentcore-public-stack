import { HttpErrorResponse } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Router } from '@angular/router';
import { v4 as uuidv4 } from 'uuid';

import { UserService } from '../../auth/user.service';
import { WorkflowRunService } from '../../session/services/handoff/workflow-run.service';
import { MessageMapService } from '../../session/services/session/message-map.service';
import { SessionPrewarmService } from '../../session/services/session/session-prewarm.service';
import { SessionService } from '../../session/services/session/session.service';
import { ToastService } from '../toast/toast.service';
import { WorkflowsApiService } from './workflows-api.service';
import { WorkflowDocument, WorkflowRef } from './workflow.models';

/** Where a saved workflow's sheet opens. */
export interface WorkflowStartOptions {
  /** Open the sheet in this conversation. Absent: a new conversation titled with the workflow's title. */
  sessionId?: string;
}

/**
 * Starting a saved workflow (docs/specs/workflow-designer.md §8.3, PR-D.2): the one way in for the
 * composer's `+` menu, and for the Workflows page's Start and Try in a chat (D.3).
 *
 * It reads the workflow as the caller (`GET /workflows/{skillId}/{name}`), then opens the run sheet
 * through `WorkflowRunService.open`, pre-filled with the workflow, so there is no second sheet. It
 * shows again on every start. Nothing runs until the user presses Start on it.
 *
 * With a `sessionId` the sheet opens there; a conversation this tab started that has no turns yet
 * gets its row first, titled with the workflow. Without one it starts a new conversation: `PUT /sessions/{id}/metadata` creates the
 * row with the workflow's title (run create needs only that row, not turns), the sidebar gets the
 * row, and the tab navigates there before the sheet opens. The first turn keeps the title, because
 * the Runtime names a conversation only when its row is new.
 *
 * The signature is stable: D.3 calls `start(ref)` for Start and `start(ref)` for Try in a chat, and
 * `start(ref, { sessionId })` from inside a conversation.
 */
@Injectable({ providedIn: 'root' })
export class WorkflowStartService {
  private readonly api = inject(WorkflowsApiService);
  private readonly runs = inject(WorkflowRunService);
  private readonly sessions = inject(SessionService);
  private readonly messages = inject(MessageMapService);
  private readonly prewarm = inject(SessionPrewarmService);
  private readonly users = inject(UserService);
  private readonly router = inject(Router);
  private readonly toast = inject(ToastService);

  /**
   * Open the sheet for `ref`, in `options.sessionId` or a new conversation. Resolves with the
   * conversation it opened in, or `null` when the workflow couldn't be read (a toast says why) or
   * the new conversation couldn't be created. Never throws.
   */
  async start(ref: WorkflowRef, options: WorkflowStartOptions = {}): Promise<string | null> {
    let document: WorkflowDocument;
    try {
      // Read first, so a workflow that is gone never leaves an empty conversation behind.
      document = await this.api.get(ref);
    } catch (error) {
      this.toast.error('This workflow couldn’t be opened', describeReadError(error));
      return null;
    }

    const title = document.workflow.title;
    const sessionId = options.sessionId
      ? await this.ensureRow(options.sessionId, title)
      : await this.newConversation(title);
    if (!sessionId) return null;
    void this.runs.open(sessionId, {
      agents: document.workflow.steps.map((step) => ({
        agentId: step.agentId,
        agentName: step.agent.name ?? '',
        emoji: step.agent.emoji,
        iconUrl: step.agent.imageUrl,
      })),
      request: document.workflow.request ?? '',
      workflow: { ref: { skillId: ref.skillId, name: ref.name }, document },
    });
    return sessionId;
  }

  /**
   * A conversation this tab started that has no turns yet (one staged by an attachment, or one
   * this service made) has no row for run create to read: give it one, titled with the workflow.
   * Any other conversation is used as it is, title and all.
   */
  private async ensureRow(sessionId: string, title: string): Promise<string | null> {
    if (!this.sessions.isNewSession(sessionId) || this.messages.getMessagesForSession(sessionId)().length > 0) {
      return sessionId;
    }
    try {
      await this.sessions.updateSessionMetadata(sessionId, { title });
    } catch {
      this.toast.error('The conversation couldn’t be started', 'Try again.');
      return null;
    }
    this.sessions.applyServerTitle(sessionId, title);
    return sessionId;
  }

  /** A conversation with no turns, titled `title`, shown in the sidebar and navigated to. */
  private async newConversation(title: string): Promise<string | null> {
    const sessionId = this.prewarm.claimNewConversationId() ?? uuidv4();
    try {
      await this.sessions.updateSessionMetadata(sessionId, { title });
    } catch {
      this.toast.error('The conversation couldn’t be started', 'Try again.');
      return null;
    }
    const user = this.users.currentUser();
    this.sessions.addSessionToCache(sessionId, user?.user_id || 'anonymous', title);
    await this.router.navigate(['s', sessionId]);
    return sessionId;
  }
}

function describeReadError(error: unknown): string {
  if (error instanceof HttpErrorResponse && error.status === 404) {
    return 'It may have been deleted, or it isn’t shared with you any more.';
  }
  if (error instanceof HttpErrorResponse && error.status === 400) {
    return 'Its file has a problem. Open it in the designer to fix it.';
  }
  return 'Try again.';
}
