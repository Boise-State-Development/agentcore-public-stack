import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { DatePipe } from '@angular/common';
import { Router, RouterLink } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowTopRightOnSquare, heroChatBubbleLeftRight, heroDocumentDuplicate, heroTrash } from '@ng-icons/heroicons/outline';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../components/confirmation-dialog/confirmation-dialog.component';
import { TooltipDirective } from '../../components/tooltip/tooltip.directive';
import { SessionMetadata } from '../../session/services/models/session-metadata.model';
import { SessionService } from '../../session/services/session/session.service';
import { ShareService } from '../../session/services/share/share.service';
import { parseIso } from '../../utils/date';
import { Project, SharedTask } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';

const PAGE_SIZE = 20;

/**
 * A project's Tasks tab (shared-projects §5, PR-1.6).
 *
 * Two lists, because a task is private until its owner shares it:
 *  - **Your tasks** — the caller's own sessions in the project, newest first,
 *    paged by the backend's value cursor. Each opens on the project's agent.
 *  - **Shared with the project** — one entry per task a member shared to
 *    "Project members". Opens in the existing `/shared/{shareId}` view; anyone
 *    can continue one in a task of their own (the share export), and its sharer
 *    can revoke it.
 */
@Component({
  selector: 'app-project-tasks',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe, NgIcon, RouterLink, TooltipDirective],
  providers: [provideIcons({ heroArrowTopRightOnSquare, heroChatBubbleLeftRight, heroDocumentDuplicate, heroTrash })],
  template: `
    <div class="max-w-3xl space-y-10">
      <section aria-labelledby="my-tasks-heading">
        <div class="flex items-baseline justify-between gap-3">
          <h2 id="my-tasks-heading" class="text-base/7 font-semibold text-gray-900 dark:text-white">Your tasks</h2>
          @if (!archived()) {
            <a
              [routerLink]="['/projects', project().projectId, 'overview']"
              class="rounded-sm text-sm/6 font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark"
            >
              Start a task
            </a>
          }
        </div>
        <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
          Only you can see these. To show one to the project, open it and share it with “Project members”.
        </p>

        @if (tasksError()) {
          <p role="alert" class="mt-3 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ tasksError() }}</p>
        }

        @if (tasksLoading() && tasks().length === 0) {
          <div class="mt-3 h-24 animate-pulse rounded-2xl bg-gray-100 dark:bg-gray-800" aria-busy="true"></div>
        } @else if (tasks().length === 0 && !tasksError()) {
          <div class="mt-3 rounded-2xl border border-dashed border-gray-300 p-6 text-center dark:border-gray-700">
            <ng-icon name="heroChatBubbleLeftRight" class="mx-auto size-6 text-gray-400 dark:text-gray-500" aria-hidden="true" />
            <p class="mt-2 text-sm/6 text-gray-600 dark:text-gray-400">You haven’t started a task in this project yet.</p>
          </div>
        } @else if (tasks().length > 0) {
          <ul class="mt-3 divide-y divide-gray-200 overflow-hidden rounded-2xl border border-gray-200 bg-white dark:divide-gray-700 dark:border-gray-700 dark:bg-gray-800">
            @for (task of tasks(); track task.sessionId) {
              <li>
                <a
                  [routerLink]="['/s', task.sessionId]"
                  [queryParams]="taskQueryParams()"
                  class="flex items-center gap-3 px-4 py-3 hover:bg-gray-50 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-gray-700/50"
                >
                  <span class="min-w-0 flex-1 truncate text-sm/6 font-medium text-gray-900 dark:text-white">{{ task.title || 'Untitled task' }}</span>
                  <span class="shrink-0 text-xs/5 text-gray-600 dark:text-gray-400">{{ when(task) | date: 'mediumDate' }}</span>
                </a>
              </li>
            }
          </ul>
          @if (nextToken()) {
            <button
              type="button"
              (click)="loadMoreTasks()"
              [disabled]="tasksLoading()"
              class="mt-3 rounded-2xl border border-gray-300 bg-white px-3.5 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-60 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
            >
              {{ tasksLoading() ? 'Loading…' : 'Show more' }}
            </button>
          }
        }
      </section>

      <section aria-labelledby="shared-tasks-heading">
        <h2 id="shared-tasks-heading" class="text-base/7 font-semibold text-gray-900 dark:text-white">Shared with the project</h2>
        <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
          A snapshot of each task at the moment it was shared. Continue one to pick it up in a task of your own.
        </p>

        @if (sharedError()) {
          <p role="alert" class="mt-3 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ sharedError() }}</p>
        }

        @if (sharedLoading() && shared().length === 0) {
          <div class="mt-3 h-24 animate-pulse rounded-2xl bg-gray-100 dark:bg-gray-800" aria-busy="true"></div>
        } @else if (shared().length === 0 && !sharedError()) {
          <div class="mt-3 rounded-2xl border border-dashed border-gray-300 p-6 text-center dark:border-gray-700">
            <p class="text-sm/6 text-gray-600 dark:text-gray-400">No one has shared a task with the project yet.</p>
          </div>
        } @else if (shared().length > 0) {
          <ul class="mt-3 divide-y divide-gray-200 overflow-hidden rounded-2xl border border-gray-200 bg-white dark:divide-gray-700 dark:border-gray-700 dark:bg-gray-800">
            @for (task of shared(); track task.shareId) {
              <li class="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3">
                <div class="min-w-0 flex-1">
                  <a
                    [routerLink]="['/shared', task.shareId]"
                    class="block truncate rounded-sm text-sm/6 font-medium text-gray-900 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-white"
                  >
                    {{ task.title || 'Untitled task' }}
                  </a>
                  <p class="text-xs/5 text-gray-600 dark:text-gray-400">
                    Shared by {{ task.isMine ? 'you' : task.sharedByEmail }} · {{ sharedAt(task) | date: 'mediumDate' }}
                  </p>
                </div>
                <div class="flex items-center gap-1">
                  <button
                    type="button"
                    (click)="continueTask(task)"
                    [disabled]="busyShareId() !== null"
                    class="inline-flex items-center gap-1.5 rounded-2xl border border-gray-300 bg-white px-3 py-1 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-60 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
                  >
                    <ng-icon name="heroDocumentDuplicate" class="size-4" aria-hidden="true" />
                    {{ busyShareId() === task.shareId && busyAction() === 'continue' ? 'Copying…' : 'Continue in my own task' }}
                  </button>
                  @if (task.isMine) {
                    <button
                      type="button"
                      (click)="revoke(task)"
                      [disabled]="busyShareId() !== null"
                      [appTooltip]="'Stop sharing'"
                      appTooltipPosition="top"
                      [attr.aria-label]="'Stop sharing ' + (task.title || 'Untitled task')"
                      class="flex size-8 items-center justify-center rounded-2xl text-gray-500 hover:bg-state-danger-50 hover:text-state-danger-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-600 disabled:cursor-not-allowed disabled:opacity-60 dark:text-gray-400 dark:hover:bg-state-danger-900/20 dark:hover:text-state-danger-400"
                    >
                      <ng-icon name="heroTrash" class="size-4" aria-hidden="true" />
                    </button>
                  }
                </div>
              </li>
            }
          </ul>
        }
      </section>
    </div>
  `,
})
export class ProjectTasksComponent {
  private api = inject(ProjectApiService);
  private shares = inject(ShareService);
  private sessions = inject(SessionService);
  private dialog = inject(Dialog);
  private router = inject(Router);

  readonly project = input.required<Project>();

  protected readonly tasks = signal<SessionMetadata[]>([]);
  protected readonly nextToken = signal<string | null>(null);
  protected readonly tasksLoading = signal(false);
  protected readonly tasksError = signal<string | null>(null);

  protected readonly shared = signal<SharedTask[]>([]);
  protected readonly sharedLoading = signal(false);
  protected readonly sharedError = signal<string | null>(null);
  protected readonly busyShareId = signal<string | null>(null);
  protected readonly busyAction = signal<'continue' | 'revoke' | null>(null);

  protected readonly archived = computed(() => this.project().status === 'archived');
  /** A task opens on the project's agent, like the sidebar's `getSessionQueryParams`. */
  protected readonly taskQueryParams = computed(() => ({ assistantId: this.project().harnessAgentId }));

  private readonly projectId = computed(() => this.project().projectId);

  constructor() {
    effect(() => {
      const id = this.projectId();
      untracked(() => {
        this.tasks.set([]);
        this.shared.set([]);
        void this.loadTasks(id, null);
        void this.loadShared(id);
      });
    });
  }

  protected when(task: SessionMetadata): Date {
    return parseIso(task.lastMessageAt || task.createdAt);
  }

  protected sharedAt(task: SharedTask): Date {
    return parseIso(task.sharedAt);
  }

  protected loadMoreTasks(): void {
    void this.loadTasks(this.projectId(), this.nextToken());
  }

  private async loadTasks(projectId: string, cursor: string | null): Promise<void> {
    this.tasksLoading.set(true);
    this.tasksError.set(null);
    try {
      const page = await firstValueFrom(this.api.tasks(projectId, PAGE_SIZE, cursor));
      if (projectId !== this.projectId()) return;
      this.tasks.update(list => (cursor ? [...list, ...page.sessions] : page.sessions));
      this.nextToken.set(page.nextToken ?? null);
    } catch (err) {
      this.tasksError.set(projectErrorMessage(err, 'Your tasks could not be loaded.'));
    } finally {
      this.tasksLoading.set(false);
    }
  }

  private async loadShared(projectId: string): Promise<void> {
    this.sharedLoading.set(true);
    this.sharedError.set(null);
    try {
      const response = await firstValueFrom(this.api.sharedTasks(projectId));
      if (projectId !== this.projectId()) return;
      this.shared.set(response.tasks);
    } catch (err) {
      this.sharedError.set(projectErrorMessage(err, 'Shared tasks could not be loaded.'));
    } finally {
      this.sharedLoading.set(false);
    }
  }

  /**
   * Fork the snapshot into a task of the caller's. For a member of an active
   * project the backend keeps the project and binds the project's current agent,
   * so the copy opens on that agent and lands in Your tasks.
   */
  protected async continueTask(task: SharedTask): Promise<void> {
    this.busyShareId.set(task.shareId);
    this.busyAction.set('continue');
    this.sharedError.set(null);
    try {
      const fork = await this.shares.exportSharedConversation(task.shareId, { suppressErrorToast: true });
      this.sessions.refreshSessions();
      await this.router.navigate(['/s', fork.sessionId], {
        queryParams: this.archived() ? {} : this.taskQueryParams(),
      });
    } catch (err) {
      this.sharedError.set(projectErrorMessage(err, 'The task could not be copied. Please try again.'));
    } finally {
      this.busyShareId.set(null);
      this.busyAction.set(null);
    }
  }

  protected async revoke(task: SharedTask): Promise<void> {
    const ref = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: 'Stop sharing this task?',
        message: `“${task.title || 'Untitled task'}” will no longer be listed in the project, and its link will stop working for members. Copies people already made stay theirs.`,
        confirmText: 'Stop sharing',
        cancelText: 'Cancel',
        destructive: true,
      } as ConfirmationDialogData,
    });
    if (!(await firstValueFrom(ref.closed))) return;

    this.busyShareId.set(task.shareId);
    this.busyAction.set('revoke');
    this.sharedError.set(null);
    try {
      await this.shares.revokeShare(task.shareId, { suppressErrorToast: true });
      // The pointer falls back to an older project share of the same task, if any,
      // so re-read rather than just dropping the row.
      await this.loadShared(this.projectId());
    } catch (err) {
      this.sharedError.set(projectErrorMessage(err, 'The share could not be removed. Please try again.'));
    } finally {
      this.busyShareId.set(null);
      this.busyAction.set(null);
    }
  }
}
