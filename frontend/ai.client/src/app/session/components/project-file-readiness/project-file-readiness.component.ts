import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  effect,
  inject,
  input,
  signal,
  untracked,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowPath, heroCheckCircle, heroExclamationTriangle, heroXMark } from '@ng-icons/heroicons/outline';
import { PROCESSING_STATUSES } from '../../../assistants/models/document.model';
import { ProjectDocument, ProjectDocumentsResponse } from '../../../projects/models/project.model';
import { ProjectApiService } from '../../../projects/services/project-api.service';

/** First re-check after a file is seen preparing, then backing off to the cap. */
export const READINESS_POLL_START_MS = 3000;
export const READINESS_POLL_MAX_MS = 15000;
/** How long "All N project files are ready" stays up before the strip goes. */
export const READY_FLASH_MS = 6000;
/** Pages of 100 read per check; a project past 500 files is counted from the first 500. */
const MAX_PAGES = 5;

/**
 * Above a project chat's composer: whether the project's files can be used yet.
 *
 * Retrieval reads a project file only once it is ready, so a member who asks
 * about files still being prepared gets an answer without them. On prod
 * (2026-11-03) a new project's 11 files sat at "Setting up" for 14 minutes and
 * nothing on the chat screen said so. This shows "Preparing 3 of 11 project
 * files…" while any are, "All 11 project files are ready" for a moment when they
 * get there, and a dismissible line for files that couldn't be added. Nothing at
 * all when every file is ready, which is the normal case.
 *
 * It polls the project's file list only while something is preparing, backs off
 * to {@link READINESS_POLL_MAX_MS}, skips checks while the tab is hidden, and
 * re-checks once when the tab comes back (a file may have been added elsewhere).
 */
@Component({
  selector: 'app-project-file-readiness',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink],
  providers: [provideIcons({ heroArrowPath, heroCheckCircle, heroExclamationTriangle, heroXMark })],
  template: `
    <div role="status" aria-live="polite">
      @if (pending() > 0) {
        <div class="mb-2 flex items-start gap-2 rounded-2xl border border-gray-200 bg-white px-3 py-2 text-xs/5 text-gray-700 shadow-xs dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300">
          <ng-icon
            name="heroArrowPath"
            class="mt-0.5 size-4 shrink-0 animate-spin text-gray-500 motion-reduce:animate-none dark:text-gray-400"
            aria-hidden="true"
          />
          <p class="min-w-0 flex-1">
            Preparing {{ pending() }} of {{ total() }} project {{ total() === 1 ? 'file' : 'files' }}.
            The assistant can't use {{ pending() === 1 ? 'it' : 'them' }} until {{ pending() === 1 ? 'it is' : 'they are' }} ready,
            usually within a few minutes.
            @if (failed() > 0) {
              {{ failed() }} couldn't be added.
            }
            <a [routerLink]="filesLink()" class="font-medium text-primary-accessible underline underline-offset-2 hover:no-underline dark:text-primary-50">View files</a>
          </p>
        </div>
      } @else if (justReady()) {
        <div class="mb-2 flex items-start gap-2 rounded-2xl border border-gray-200 bg-white px-3 py-2 text-xs/5 text-gray-700 shadow-xs dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300">
          <ng-icon name="heroCheckCircle" class="mt-0.5 size-4 shrink-0 text-state-success-600 dark:text-state-success-400" aria-hidden="true" />
          <p class="min-w-0 flex-1">
            @if (failed() > 0) {
              {{ ready() }} of {{ total() }} project files {{ ready() === 1 ? 'is' : 'are' }} ready.
              {{ failed() }} couldn't be added.
            } @else if (total() === 1) {
              The project file is ready.
            } @else {
              All {{ total() }} project files are ready.
            }
          </p>
        </div>
      } @else if (failed() > 0 && !failedDismissed()) {
        <div class="mb-2 flex items-start gap-2 rounded-2xl border border-gray-200 bg-white px-3 py-2 text-xs/5 text-gray-700 shadow-xs dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300">
          <ng-icon name="heroExclamationTriangle" class="mt-0.5 size-4 shrink-0 text-state-warning-600 dark:text-state-warning-400" aria-hidden="true" />
          <p class="min-w-0 flex-1">
            {{ failed() }} project {{ failed() === 1 ? "file couldn't" : "files couldn't" }} be added, so the assistant can't use
            {{ failed() === 1 ? 'it' : 'them' }}.
            <a [routerLink]="filesLink()" class="font-medium text-primary-accessible underline underline-offset-2 hover:no-underline dark:text-primary-50">Upload again from Files</a>
          </p>
          <button
            type="button"
            class="-m-1 shrink-0 rounded-md p-1 text-gray-500 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible dark:text-gray-400 dark:hover:text-gray-200"
            aria-label="Dismiss notice"
            (click)="dismissFailed()"
          >
            <ng-icon name="heroXMark" class="size-4" aria-hidden="true" />
          </button>
        </div>
      }
    </div>
  `,
})
export class ProjectFileReadinessComponent {
  private readonly api = inject(ProjectApiService);

  readonly projectId = input.required<string>();

  private readonly documents = signal<ProjectDocument[]>([]);
  protected readonly justReady = signal(false);
  private readonly dismissedFor = signal<string | null>(null);

  protected readonly total = computed(() => this.documents().length);
  protected readonly pending = computed(
    () => this.documents().filter(d => PROCESSING_STATUSES.includes(d.status)).length,
  );
  protected readonly failed = computed(() => this.documents().filter(d => d.status === 'failed').length);
  protected readonly ready = computed(() => this.total() - this.pending() - this.failed());
  protected readonly failedDismissed = computed(() => this.dismissedFor() === this.projectId());
  protected readonly filesLink = computed(() => ['/projects', this.projectId(), 'files']);

  /** Bumped on project change and destroy; a check from an older generation drops its result. */
  private generation = 0;
  private pollTimer: ReturnType<typeof setTimeout> | null = null;
  private flashTimer: ReturnType<typeof setTimeout> | null = null;
  private interval = READINESS_POLL_START_MS;

  constructor() {
    effect(() => {
      const id = this.projectId();
      untracked(() => {
        this.reset();
        void this.check(id, this.generation);
      });
    });

    const onVisibility = () => {
      if (document.visibilityState !== 'visible') return;
      this.clearPoll();
      this.interval = READINESS_POLL_START_MS;
      void this.check(this.projectId(), this.generation);
    };
    document.addEventListener('visibilitychange', onVisibility);
    inject(DestroyRef).onDestroy(() => {
      document.removeEventListener('visibilitychange', onVisibility);
      this.generation++;
      this.clearPoll();
      this.clearFlash();
    });
  }

  protected dismissFailed(): void {
    this.dismissedFor.set(this.projectId());
  }

  private reset(): void {
    this.generation++;
    this.clearPoll();
    this.clearFlash();
    this.interval = READINESS_POLL_START_MS;
    this.documents.set([]);
    this.justReady.set(false);
  }

  private async check(projectId: string, generation: number): Promise<void> {
    let documents: ProjectDocument[];
    try {
      documents = await this.list(projectId);
    } catch {
      // Quiet on failure: this is a progress hint, and the Files page reports errors.
      return;
    }
    if (generation !== this.generation) return;

    const wasPending = this.pending() > 0;
    this.documents.set(documents);
    if (this.pending() > 0) {
      this.schedule(projectId, generation);
    } else if (wasPending) {
      this.flashReady();
    }
  }

  private async list(projectId: string): Promise<ProjectDocument[]> {
    const all: ProjectDocument[] = [];
    let token: string | null = null;
    for (let page = 0; page < MAX_PAGES; page++) {
      const response: ProjectDocumentsResponse = await firstValueFrom(this.api.files(projectId, 100, token));
      all.push(...response.documents);
      token = response.nextToken ?? null;
      if (!token) break;
    }
    return all;
  }

  private schedule(projectId: string, generation: number): void {
    this.clearPoll();
    this.pollTimer = setTimeout(() => {
      this.pollTimer = null;
      if (generation !== this.generation) return;
      // Nobody is looking: wait, and let the tab's return re-check.
      if (document.visibilityState === 'hidden') return;
      void this.check(projectId, generation);
    }, this.interval);
    this.interval = Math.min(this.interval * 1.5, READINESS_POLL_MAX_MS);
  }

  private flashReady(): void {
    this.justReady.set(true);
    this.clearFlash();
    this.flashTimer = setTimeout(() => {
      this.flashTimer = null;
      this.justReady.set(false);
    }, READY_FLASH_MS);
  }

  private clearPoll(): void {
    if (this.pollTimer) clearTimeout(this.pollTimer);
    this.pollTimer = null;
  }

  private clearFlash(): void {
    if (this.flashTimer) clearTimeout(this.flashTimer);
    this.flashTimer = null;
  }
}
