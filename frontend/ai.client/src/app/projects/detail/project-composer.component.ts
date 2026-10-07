import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowUp } from '@ng-icons/heroicons/outline';
import { ChatRequestService } from '../../session/services/chat/chat-request.service';
import { Project } from '../models/project.model';

/**
 * Start a task in a project.
 *
 * Sending starts a new conversation bound to the project's agent (its
 * `harnessAgentId`); the backend marks that session with the project, so it is a
 * task in this project from its first turn.
 */
@Component({
  selector: 'app-project-composer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroArrowUp })],
  template: `
    <section aria-labelledby="new-task-heading">
      <h2 id="new-task-heading" class="sr-only">Start a task</h2>
      <form
        (submit)="$event.preventDefault(); send()"
        class="rounded-2xl bg-white shadow-xl outline -outline-offset-1 outline-gray-300 transition-[outline-color,box-shadow] duration-200 focus-within:outline-2 focus-within:outline-primary-500 focus-within:ring-2 focus-within:ring-primary-500/20 dark:bg-slate-800 dark:outline-white/10 dark:focus-within:outline-gray-400 dark:focus-within:ring-gray-400/30"
      >
        <label for="project-composer" class="sr-only">Message</label>
        <textarea
          id="project-composer"
          rows="2"
          [value]="draft()"
          (input)="draft.set($any($event.target).value)"
          (keydown.enter)="onEnter($event)"
          [disabled]="archived()"
          [placeholder]="placeholder()"
          class="block w-full resize-none border-0 bg-transparent px-4 pt-4 pb-1 text-base/6 text-gray-900 placeholder:text-gray-500 focus:outline-none focus:ring-0 disabled:cursor-not-allowed dark:text-white dark:placeholder:text-gray-400"
        ></textarea>
        <div class="flex items-center justify-end gap-2 px-3 pt-1 pb-3">
          <button
            type="submit"
            [disabled]="!canSend()"
            aria-label="Start task"
            class="grid size-9 shrink-0 place-items-center rounded-xl bg-primary-accessible text-white transition hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <ng-icon name="heroArrowUp" class="size-5" aria-hidden="true" />
          </button>
        </div>
      </form>
      <div class="mt-2.5 flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-1 text-xs/5 text-gray-600 dark:text-gray-400">
        <p>Tasks are private to you until you share one with the project.</p>
        @if (modelLabel(); as model) {
          <p class="truncate"><span class="sr-only">Model: </span>{{ model }}</p>
        }
      </div>
      @if (sendError()) {
        <p role="alert" class="mt-2 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ sendError() }}</p>
      }
    </section>
  `,
})
export class ProjectComposerComponent {
  private chatRequest = inject(ChatRequestService);

  readonly project = input.required<Project>();
  /** What the task will run on, shown beneath the composer; null hides it. */
  readonly modelLabel = input<string | null>(null);

  protected readonly draft = signal('');
  protected readonly sending = signal(false);
  protected readonly sendError = signal<string | null>(null);

  protected readonly archived = computed(() => this.project().status === 'archived');
  protected readonly canSend = computed(() => !this.archived() && !this.sending() && this.draft().trim().length > 0);
  protected readonly placeholder = computed(() =>
    this.archived() ? 'This project is archived.' : `New task in ${this.project().name}`,
  );

  protected onEnter(event: Event): void {
    const e = event as KeyboardEvent;
    if (e.shiftKey || e.isComposing) return;
    e.preventDefault();
    void this.send();
  }

  protected async send(): Promise<void> {
    if (!this.canSend()) return;
    this.sending.set(true);
    this.sendError.set(null);
    try {
      await this.chatRequest.submitChatRequest(this.draft().trim(), null, undefined, this.project().harnessAgentId);
      this.draft.set('');
    } catch {
      this.sendError.set('The task could not be started. Please try again.');
    } finally {
      this.sending.set(false);
    }
  }
}
