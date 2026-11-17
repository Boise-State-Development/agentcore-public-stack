import { ChangeDetectionStrategy, Component, DestroyRef, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { Router } from '@angular/router';
import { v4 as uuidv4 } from 'uuid';
import { AgentBindingLocksService } from '../../agents/services/agent-binding-locks.service';
import { AgentService } from '../../agents/services/agent.service';
import { UserService } from '../../auth/user.service';
import { ChatInputComponent, ComposerProject } from '../../session/components/chat-input/chat-input.component';
import { ChatRequestService } from '../../session/services/chat/chat-request.service';
import { FolderAttachmentData } from '../../session/services/models/message.model';
import { ConversationBindingService } from '../../session/services/session/conversation-binding.service';
import { SessionPrewarmService } from '../../session/services/session/session-prewarm.service';
import { SessionService } from '../../session/services/session/session.service';
import { VoiceChatService } from '../../session/services/voice/voice-chat.service';
import { Project } from '../models/project.model';

/** What `app-chat-input` emits on send; the fields a project task carries. */
interface ComposerSend {
  content: string;
  fileUploadIds?: string[];
  invokedSkillIds?: string[];
  folders?: FolderAttachmentData[];
  pastedChars?: number;
}

/**
 * Start a task in a project.
 *
 * The conversation page's own composer (`app-chat-input`), so a project task
 * starts with everything a conversation offers: attachments (picker, drag and
 * drop, paste, Drive), `/` skill commands, dictation, the model picker and,
 * while the project allows it (#2021), voice mode. `@`-mentions turn themselves
 * off for a project.
 *
 * Voice starts here, on the user's click, and once it is live the page moves to
 * the new conversation (`/s/{id}?project=`), whose page shows the voice panel and
 * keeps the transcript. The session is the project's from the first frame: the
 * connect names the project's agent.
 *
 * Sending starts a new conversation bound to the project's agent (its
 * `harnessAgentId`); the backend marks that session with the project, so it is a
 * task in this project from its first turn. The pickers are locked to the
 * harness's bindings while this composer is up, exactly as they are in the
 * project's conversations, so the `/` menu offers the project's skills and the
 * model picker shows the model the project runs on.
 */
@Component({
  selector: 'app-project-composer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ChatInputComponent],
  template: `
    <section aria-labelledby="new-task-heading">
      <h2 id="new-task-heading" class="sr-only">Start a conversation</h2>
      @if (archived()) {
        <div
          aria-disabled="true"
          class="rounded-2xl bg-white px-4 py-5 text-base/6 text-gray-500 shadow-xl outline -outline-offset-1 outline-gray-300 dark:bg-slate-800 dark:text-gray-400 dark:outline-white/10"
        >
          This project is archived.
        </div>
      } @else {
        <app-chat-input
          [sessionId]="stagedSessionId()"
          [project]="composerProject()"
          [allowFolderAttachments]="true"
          [showVoiceControl]="showVoice()"
          [voiceAgentId]="harnessAgentId()"
          [showAnnouncements]="false"
          [autoFocus]="false"
          (fileAttached)="onFileAttached()"
          (messageSubmitted)="send($event)"
        />
        <p class="mt-1 px-1 text-xs/5 text-gray-600 dark:text-gray-400">
          Conversations are private to you until you share one with the project.
        </p>
      }
      @if (sendError()) {
        <p role="alert" class="mt-2 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ sendError() }}</p>
      }
    </section>
  `,
})
export class ProjectComposerComponent {
  private readonly chatRequest = inject(ChatRequestService);
  private readonly binding = inject(ConversationBindingService);
  private readonly agents = inject(AgentService);
  private readonly bindingLocks = inject(AgentBindingLocksService);
  private readonly sessions = inject(SessionService);
  private readonly prewarm = inject(SessionPrewarmService);
  private readonly user = inject(UserService);
  private readonly voiceChat = inject(VoiceChatService);
  private readonly router = inject(Router);

  readonly project = input.required<Project>();
  /**
   * The model the project runs on, as the page's settings rail knows it: an id,
   * null for each member's default, undefined until known. Changing the model in
   * the rail re-locks the picker here without re-reading the harness.
   */
  readonly projectModelId = input<string | null | undefined>(undefined);
  /** Whether the project's conversations can use voice mode, as the rail read it (#2021). */
  readonly voiceAvailable = input<boolean | undefined>(undefined);

  /**
   * The id a new task's attachments are filed under, minted on the first file
   * (as the conversation page does) and consumed by the send, which starts the
   * conversation on it. Null until something is attached.
   */
  protected readonly stagedSessionId = signal<string | null>(null);
  protected readonly sendError = signal<string | null>(null);

  protected readonly archived = computed(() => this.project().status === 'archived');
  protected readonly composerProject = computed<ComposerProject>(() => ({
    projectId: this.project().projectId,
    name: this.project().name,
  }));
  protected readonly harnessAgentId = computed(() => this.project().harnessAgentId);
  protected readonly showVoice = computed(() => this.voiceAvailable() === true && !this.archived());

  constructor() {
    // Lock the pickers to the harness while this composer can send to it. Moving
    // between projects reuses the page, so this follows the input.
    effect(() => {
      const agentId = this.archived() ? null : this.harnessAgentId();
      untracked(() => {
        this.bindingLocks.clear();
        if (agentId) void this.lockTo(agentId);
      });
    });
    effect(() => {
      const modelId = this.projectModelId();
      if (modelId === undefined || this.archived()) return;
      untracked(() => this.bindingLocks.lockModel(modelId));
    });
    // The locks live in root services; release them for whatever page comes next
    // (the release also puts back the user's own model). A task opened from here
    // re-applies them from its own `?project=`.
    inject(DestroyRef).onDestroy(() => this.bindingLocks.clear());

    // A voice session started from this composer moves to its conversation, where
    // the voice panel lives, once it is live (a refused mic or a refused session
    // stays here and leaves no empty conversation behind). Only a connect seen here
    // counts: one already running when the page opened belongs to another one.
    let previous = this.voiceChat.status();
    effect(() => {
      const status = this.voiceChat.status();
      const started = previous === 'connecting' && (status === 'listening' || status === 'speaking');
      previous = status;
      if (started) untracked(() => this.openVoiceConversation());
    });
  }

  private openVoiceConversation(): void {
    const sessionId = this.voiceChat.getSessionId();
    if (!sessionId || this.archived()) return;
    const project = this.project();
    // Known first, so the conversation opens as `?project=` without a fetch, and
    // marked new, so its page doesn't read metadata the backend hasn't written.
    this.binding.knowProject(project);
    this.sessions.addSessionToCache(sessionId, this.user.currentUser()?.user_id || 'anonymous');
    this.stagedSessionId.set(null);
    void this.router.navigate(['s', sessionId], {
      queryParams: this.binding.queryParamsFor(project.harnessAgentId, project.projectId),
    });
  }

  /** Best-effort, like the conversation page: a failed read leaves the pickers free. */
  private async lockTo(agentId: string): Promise<void> {
    const isCurrent = () => !this.archived() && this.harnessAgentId() === agentId;
    try {
      const agent = await this.agents.getAgent(agentId);
      if (isCurrent()) this.bindingLocks.apply(agent, isCurrent);
    } catch {
      // The backend still runs the harness's bindings whatever the pickers show.
    }
  }

  protected onFileAttached(): void {
    if (this.stagedSessionId()) return;
    // Reuse a warmed id when there is one, so the uploads and the first turn
    // share the microVM that is already starting (session prewarm).
    const id = this.prewarm.claimNewConversationId() ?? uuidv4();
    this.stagedSessionId.set(id);
    // Marks the id as new, so the conversation page opened by the send does not
    // fetch metadata for a conversation the backend has not created yet.
    this.sessions.addSessionToCache(id, this.user.currentUser()?.user_id || 'anonymous');
  }

  protected async send(message: ComposerSend): Promise<void> {
    if (this.archived()) return;
    this.sendError.set(null);
    try {
      // Known to the binding first, so the new conversation opens as `?project=`.
      this.binding.knowProject(this.project());
      await this.chatRequest.submitChatRequest(
        message.content,
        this.stagedSessionId(),
        message.fileUploadIds,
        this.project().harnessAgentId,
        undefined,
        message.invokedSkillIds,
        message.folders,
        message.pastedChars,
      );
      this.stagedSessionId.set(null);
    } catch {
      this.sendError.set('The conversation could not be started. Please try again.');
    }
  }
}
