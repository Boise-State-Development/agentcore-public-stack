import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowUp, heroDocumentText, heroUsers } from '@ng-icons/heroicons/outline';
import { ChatRequestService } from '../../session/services/chat/chat-request.service';
import { Project } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';

/**
 * A project's Overview: start a task, and see what the assistant works from.
 *
 * Sending starts a new conversation bound to the project's agent (its
 * `harnessAgentId`); the backend marks that session with the project, so it is a
 * task in this project from its first turn.
 */
@Component({
  selector: 'app-project-overview',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink],
  providers: [provideIcons({ heroArrowUp, heroDocumentText, heroUsers })],
  template: `
    <div class="grid gap-8 lg:grid-cols-[1fr_320px]">
      <section aria-labelledby="new-task-heading">
        <h2 id="new-task-heading" class="sr-only">Start a task</h2>
        <form
          (submit)="$event.preventDefault(); send()"
          class="rounded-2xl border border-gray-300 bg-white p-3 shadow-xs focus-within:border-primary-500 focus-within:ring-2 focus-within:ring-primary-500 dark:border-gray-600 dark:bg-gray-800"
        >
          <label for="project-composer" class="sr-only">Message</label>
          <textarea
            id="project-composer"
            rows="3"
            [value]="draft()"
            (input)="draft.set($any($event.target).value)"
            (keydown.enter)="onEnter($event)"
            [disabled]="archived()"
            [placeholder]="archived() ? 'This project is archived.' : 'Start a task in ' + project().name + '…'"
            class="block w-full resize-none border-0 bg-transparent px-2 py-1.5 text-sm/6 text-gray-900 placeholder:text-gray-500 focus:outline-none focus:ring-0 disabled:cursor-not-allowed dark:text-white dark:placeholder:text-gray-400"
          ></textarea>
          <div class="flex items-center justify-between gap-3 px-2 pt-2">
            <p class="text-xs/5 text-gray-600 dark:text-gray-400">
              Tasks are private to you until you share one with the project.
            </p>
            <button
              type="submit"
              [disabled]="!canSend()"
              aria-label="Start task"
              class="grid size-9 shrink-0 place-items-center rounded-2xl bg-primary-accessible text-white transition hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <ng-icon name="heroArrowUp" class="size-5" aria-hidden="true" />
            </button>
          </div>
        </form>
        @if (sendError()) {
          <p role="alert" class="mt-2 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ sendError() }}</p>
        }
      </section>

      <aside class="space-y-4" aria-label="What the assistant works from">
        <section class="rounded-2xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-gray-800/60">
          <h2 class="flex items-center gap-2 text-sm/6 font-semibold text-gray-900 dark:text-white">
            <ng-icon name="heroDocumentText" class="size-4 text-gray-500 dark:text-gray-400" aria-hidden="true" />
            Instructions
          </h2>
          @if (instructions() === null) {
            <div class="mt-2 h-12 animate-pulse rounded bg-gray-200 dark:bg-gray-700" aria-busy="true"></div>
          } @else if (instructions()) {
            <p class="mt-2 line-clamp-6 text-sm/6 whitespace-pre-line text-gray-700 dark:text-gray-300">{{ instructions() }}</p>
          } @else {
            <p class="mt-2 text-sm/6 text-gray-600 dark:text-gray-400">
              No instructions yet. The assistant uses its defaults until someone adds some.
            </p>
          }
          <a [routerLink]="['/projects', project().projectId, 'settings']" class="mt-3 inline-block rounded-sm text-sm/6 font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-50">
            {{ canEdit() ? 'Edit instructions' : 'View settings' }}
          </a>
        </section>

        <section class="rounded-2xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-gray-800/60">
          <h2 class="flex items-center gap-2 text-sm/6 font-semibold text-gray-900 dark:text-white">
            <ng-icon name="heroUsers" class="size-4 text-gray-500 dark:text-gray-400" aria-hidden="true" />
            People
          </h2>
          <p class="mt-2 text-sm/6 text-gray-700 dark:text-gray-300">{{ peopleLabel() }}</p>
          <p class="text-xs/5 text-gray-600 dark:text-gray-400">Owner: {{ project().ownerEmail }}</p>
          <a [routerLink]="['/projects', project().projectId, 'members']" class="mt-3 inline-block rounded-sm text-sm/6 font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-50">
            See members
          </a>
        </section>
      </aside>
    </div>
  `,
})
export class ProjectOverviewComponent {
  private api = inject(ProjectApiService);
  private chatRequest = inject(ChatRequestService);

  readonly project = input.required<Project>();

  protected readonly draft = signal('');
  protected readonly sending = signal(false);
  protected readonly sendError = signal<string | null>(null);
  /** Null while loading. */
  protected readonly instructions = signal<string | null>(null);

  protected readonly archived = computed(() => this.project().status === 'archived');
  protected readonly canEdit = computed(() => this.project().role !== 'viewer' && !this.archived());
  protected readonly canSend = computed(() => !this.archived() && !this.sending() && this.draft().trim().length > 0);
  protected readonly peopleLabel = computed(() => {
    const n = this.project().memberCount + 1;
    return n === 1 ? 'Just you so far' : `${n} people`;
  });

  // Keyed on the id, not the object: a tab hands back an updated project after most
  // changes, and that must not reload what it just saved.
  private readonly projectId = computed(() => this.project().projectId);

  constructor() {
    effect(() => {
      const id = this.projectId();
      untracked(() => void this.loadInstructions(id));
    });
  }

  private async loadInstructions(projectId: string): Promise<void> {
    try {
      const response = await firstValueFrom(this.api.instructions(projectId));
      this.instructions.set(response.instructions);
    } catch {
      this.instructions.set('');
    }
  }

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
