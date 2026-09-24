import { ChangeDetectionStrategy, Component, DestroyRef, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { DatePipe } from '@angular/common';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowDownTray, heroArrowUpTray, heroDocumentText, heroTrash } from '@ng-icons/heroicons/outline';
import { DocumentService, DocumentUploadError } from '../../assistants/services/document.service';
import { DocumentStatus, KbUsage, PROCESSING_STATUSES } from '../../assistants/models/document.model';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../components/confirmation-dialog/confirmation-dialog.component';
import { TooltipDirective } from '../../components/tooltip/tooltip.directive';
import { parseIso } from '../../utils/date';
import { Project, ProjectDocument } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';

/** The agent document pipeline's per-file limit (the knowledge-base section's too). */
const MAX_FILE_BYTES = 10 * 1024 * 1024;
const ACCEPT = '.pdf,.docx,.txt,.md,.html,.csv,.xls,.xlsx,.pptx';
const POLL_START_MS = 1000;
const POLL_MAX_MS = 10000;
const POLL_LIMIT_MS = 5 * 60 * 1000;

const STATUS_LABELS: Record<DocumentStatus, string> = {
  provisioning: 'Setting up',
  uploading: 'Uploading',
  chunking: 'Processing',
  embedding: 'Indexing',
  complete: 'Ready',
  failed: 'Failed',
};

interface UploadState {
  filename: string;
  progress: number;
  status: 'uploading' | 'done' | 'error';
  error?: string;
}

export function formatBytes(bytes: number): string {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${parseFloat((bytes / Math.pow(1024, i)).toFixed(1))} ${units[i]}`;
}

/**
 * A project's Files tab over `/projects/{id}/knowledge` (shared-projects §5, PR-1.5b).
 *
 * The files are the project agent's documents. Everyone in the project can see and
 * download them; editors of an active project can add and delete (`canEdit`, from the
 * server). Uploads reuse the agent pipeline's presigned S3 PUT; every other call is
 * project-scoped, because a viewer is refused on the agent's own document routes.
 */
@Component({
  selector: 'app-project-files',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe, NgIcon, TooltipDirective],
  providers: [provideIcons({ heroArrowDownTray, heroArrowUpTray, heroDocumentText, heroTrash })],
  template: `
    <div class="max-w-3xl space-y-6">
      <section aria-labelledby="files-heading">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <h2 id="files-heading" class="text-base/7 font-semibold text-gray-900 dark:text-white">
            Files
            @if (documents().length) {
              <span class="font-normal text-gray-600 dark:text-gray-400">· {{ documents().length }}</span>
            }
          </h2>
          @if (canEdit()) {
            <input #picker type="file" class="sr-only" tabindex="-1" aria-hidden="true" multiple [accept]="accept" (change)="onFilesPicked($event)" />
            <button
              type="button"
              (click)="picker.click()"
              [disabled]="uploading()"
              class="inline-flex items-center gap-2 rounded-2xl bg-primary-accessible px-3.5 py-1.5 text-sm/6 font-semibold text-white shadow-xs transition hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <ng-icon name="heroArrowUpTray" class="size-4" aria-hidden="true" />
              Add files
            </button>
          }
        </div>
        <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
          {{ notice() ?? defaultNotice() }}
          @if (!canEdit() && loaded()) {
            {{ archived() ? 'This project is archived, so files can’t be added or removed.' : 'Only editors can add or remove files.' }}
          }
        </p>
        @if (usageLabel()) {
          <p class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">{{ usageLabel() }}</p>
        }

        @if (upload(); as u) {
          <div
            role="status"
            class="mt-4 rounded-2xl border border-gray-200 bg-white p-3 text-sm/6 dark:border-gray-700 dark:bg-gray-800"
          >
            <div class="flex items-center justify-between gap-3">
              <span class="min-w-0 truncate font-medium text-gray-900 dark:text-white">{{ u.filename }}</span>
              <span
                class="shrink-0 text-xs/5"
                [class]="u.status === 'error' ? 'text-state-danger-600 dark:text-state-danger-400' : 'text-gray-600 dark:text-gray-400'"
              >
                {{ u.status === 'error' ? 'Upload failed' : u.status === 'done' ? 'Uploaded' : u.progress + '%' }}
              </span>
            </div>
            @if (u.status === 'uploading') {
              <div class="mt-2 h-1.5 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
                <div class="h-full rounded-full bg-primary-accessible transition-[width]" [style.width.%]="u.progress"></div>
              </div>
            }
            @if (u.error) {
              <p class="mt-1 text-xs/5 text-state-danger-600 dark:text-state-danger-400">{{ u.error }}</p>
            }
          </div>
        }

        @if (error()) {
          <p role="alert" class="mt-3 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ error() }}</p>
        }

        @if (loading() && documents().length === 0) {
          <div class="mt-4 h-24 animate-pulse rounded-2xl bg-gray-100 dark:bg-gray-800" aria-busy="true"></div>
        } @else if (loaded() && documents().length === 0) {
          <div class="mt-4 rounded-2xl border border-dashed border-gray-300 p-6 text-center dark:border-gray-700">
            <ng-icon name="heroDocumentText" class="mx-auto size-6 text-gray-400 dark:text-gray-500" aria-hidden="true" />
            <p class="mt-2 text-sm/6 text-gray-600 dark:text-gray-400">
              No files yet.{{ canEdit() ? ' Add documents the project’s agent should work from.' : '' }}
            </p>
          </div>
        } @else if (documents().length > 0) {
          <ul class="mt-4 divide-y divide-gray-200 overflow-hidden rounded-2xl border border-gray-200 bg-white dark:divide-gray-700 dark:border-gray-700 dark:bg-gray-800">
            @for (doc of documents(); track doc.documentId) {
              <li class="flex items-center gap-3 px-4 py-3">
                <ng-icon name="heroDocumentText" class="size-5 shrink-0 text-gray-500 dark:text-gray-400" aria-hidden="true" />
                <div class="min-w-0 flex-1">
                  <p class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">{{ doc.filename }}</p>
                  <p class="text-xs/5 text-gray-600 dark:text-gray-400">
                    Added by {{ doc.addedByEmail || 'Unknown' }} · {{ created(doc) | date: 'mediumDate' }} · {{ size(doc) }}
                  </p>
                  @if (doc.status === 'failed' && doc.errorMessage) {
                    <p class="text-xs/5 text-state-danger-600 dark:text-state-danger-400">{{ doc.errorMessage }}</p>
                  }
                </div>
                <span
                  class="shrink-0 text-xs/5 font-medium"
                  [class]="statusClass(doc.status)"
                >{{ statusLabels[doc.status] }}</span>
                @if (doc.status === 'complete') {
                  <button
                    type="button"
                    (click)="download(doc)"
                    [appTooltip]="'Download'"
                    appTooltipPosition="top"
                    [attr.aria-label]="'Download ' + doc.filename"
                    class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-500 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                  >
                    <ng-icon name="heroArrowDownTray" class="size-4" aria-hidden="true" />
                  </button>
                }
                @if (canEdit()) {
                  <button
                    type="button"
                    (click)="remove(doc)"
                    [disabled]="deletingId() === doc.documentId"
                    [appTooltip]="'Delete'"
                    appTooltipPosition="top"
                    [attr.aria-label]="'Delete ' + doc.filename"
                    class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-500 hover:bg-state-danger-50 hover:text-state-danger-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-600 disabled:cursor-not-allowed disabled:opacity-60 dark:text-gray-400 dark:hover:bg-state-danger-900/20 dark:hover:text-state-danger-400"
                  >
                    <ng-icon name="heroTrash" class="size-4" aria-hidden="true" />
                  </button>
                }
              </li>
            }
          </ul>
          @if (nextToken()) {
            <button
              type="button"
              (click)="loadMore()"
              [disabled]="loading()"
              class="mt-3 rounded-2xl border border-gray-300 bg-white px-3.5 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-60 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
            >
              {{ loading() ? 'Loading…' : 'Show more' }}
            </button>
          }
        }
      </section>
    </div>
  `,
})
export class ProjectFilesComponent {
  private api = inject(ProjectApiService);
  private documentService = inject(DocumentService);
  private dialog = inject(Dialog);

  readonly project = input.required<Project>();

  protected readonly accept = ACCEPT;
  protected readonly statusLabels = STATUS_LABELS;

  protected readonly documents = signal<ProjectDocument[]>([]);
  protected readonly nextToken = signal<string | null>(null);
  protected readonly kbUsage = signal<KbUsage | null>(null);
  protected readonly canEdit = signal(false);
  protected readonly loading = signal(false);
  protected readonly loaded = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly upload = signal<UploadState | null>(null);
  protected readonly uploading = computed(() => this.upload()?.status === 'uploading');
  protected readonly deletingId = signal<string | null>(null);
  /** The server's sentence, once an upload has returned it. */
  protected readonly notice = signal<string | null>(null);

  protected readonly archived = computed(() => this.project().status === 'archived');
  /** Said before anything is uploaded; the server's `notice` replaces it at upload. */
  protected readonly defaultNotice = computed(
    () => `Everyone in ${this.project().name} can open these files, and the project’s agent can use them to answer anyone in the project.`,
  );
  protected readonly usageLabel = computed(() => {
    const usage = this.kbUsage();
    if (!usage?.cap) return null;
    return `${formatBytes(usage.storedBytes + usage.reservedBytes)} of ${formatBytes(usage.cap)} used`;
  });

  private readonly projectId = computed(() => this.project().projectId);
  /** Documents being polled to a terminal status; cleared on project change and destroy. */
  private polling = new Set<string>();
  private generation = 0;

  constructor() {
    effect(() => {
      const id = this.projectId();
      untracked(() => {
        this.generation++;
        this.polling.clear();
        this.documents.set([]);
        this.notice.set(null);
        this.upload.set(null);
        this.loaded.set(false);
        void this.load(id, null);
      });
    });
    inject(DestroyRef).onDestroy(() => {
      this.generation++;
      this.polling.clear();
    });
  }

  protected created(doc: ProjectDocument): Date {
    return parseIso(doc.createdAt);
  }

  protected size(doc: ProjectDocument): string {
    return formatBytes(doc.sizeBytes);
  }

  protected statusClass(status: DocumentStatus): string {
    if (status === 'complete') return 'text-state-success-700 dark:text-state-success-400';
    if (status === 'failed') return 'text-state-danger-600 dark:text-state-danger-400';
    return 'text-gray-600 dark:text-gray-400';
  }

  protected loadMore(): void {
    void this.load(this.projectId(), this.nextToken());
  }

  private async load(projectId: string, cursor: string | null): Promise<void> {
    const generation = this.generation;
    this.loading.set(true);
    this.error.set(null);
    try {
      const page = await firstValueFrom(this.api.files(projectId, 100, cursor));
      if (generation !== this.generation) return;
      this.documents.update(list => (cursor ? [...list, ...page.documents] : page.documents));
      this.nextToken.set(page.nextToken ?? null);
      this.kbUsage.set(page.kbUsage ?? null);
      this.canEdit.set(page.canEdit);
      for (const doc of page.documents) {
        if (PROCESSING_STATUSES.includes(doc.status)) void this.poll(doc.documentId);
      }
    } catch (err) {
      if (generation === this.generation) this.error.set(projectErrorMessage(err, 'The project’s files could not be loaded.'));
    } finally {
      if (generation === this.generation) {
        this.loading.set(false);
        this.loaded.set(true);
      }
    }
  }

  protected async onFilesPicked(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    input.value = '';
    for (const file of files) {
      if (!(await this.uploadOne(file))) break;
    }
  }

  /** Upload one file; false when it failed (the rest of a batch is skipped). */
  private async uploadOne(file: File): Promise<boolean> {
    const projectId = this.projectId();
    const generation = this.generation;
    if (file.size > MAX_FILE_BYTES) {
      this.upload.set({
        filename: file.name,
        progress: 0,
        status: 'error',
        error: `Files can be up to ${formatBytes(MAX_FILE_BYTES)}. This one is ${formatBytes(file.size)}.`,
      });
      return false;
    }

    this.upload.set({ filename: file.name, progress: 0, status: 'uploading' });
    let documentId: string | null = null;
    try {
      const started = await firstValueFrom(
        this.api.fileUploadUrl(projectId, {
          filename: file.name,
          contentType: file.type || 'application/octet-stream',
          sizeBytes: file.size,
        }),
      );
      documentId = started.documentId;
      this.notice.set(started.notice);
      await this.documentService.uploadToS3(started.uploadUrl, file, progress =>
        this.upload.update(u => (u ? { ...u, progress } : u)),
      );
      if (generation !== this.generation) return false;
      this.upload.set({ filename: file.name, progress: 100, status: 'done' });
      await this.load(projectId, null);
      return true;
    } catch (err) {
      const message =
        err instanceof DocumentUploadError ? 'The file could not be uploaded. Please try again.' : projectErrorMessage(err, 'The file could not be uploaded.');
      if (generation === this.generation) {
        this.upload.set({ filename: file.name, progress: this.upload()?.progress ?? 0, status: 'error', error: message });
      }
      if (documentId) {
        // Otherwise the row sits in `uploading` until the stale-document sweep fails it.
        const details = err instanceof DocumentUploadError ? JSON.stringify(err.details ?? {}) : undefined;
        try {
          await firstValueFrom(this.api.reportFileUploadFailure(projectId, documentId, message, details));
        } catch {
          // Best effort, like the agent editor: the stale-document timeout still catches it.
        }
        if (generation === this.generation) await this.load(projectId, null);
      }
      return false;
    }
  }

  /** Poll one document until it finishes processing, updating its row in place. */
  private async poll(documentId: string): Promise<void> {
    if (this.polling.has(documentId)) return;
    this.polling.add(documentId);
    const projectId = this.projectId();
    const started = Date.now();
    let interval = POLL_START_MS;
    try {
      while (this.polling.has(documentId) && Date.now() - started < POLL_LIMIT_MS) {
        await new Promise(resolve => setTimeout(resolve, interval));
        if (!this.polling.has(documentId)) return;
        let doc: ProjectDocument;
        try {
          doc = await firstValueFrom(this.api.file(projectId, documentId));
        } catch {
          return;
        }
        if (!this.polling.has(documentId)) return;
        this.documents.update(list => list.map(d => (d.documentId === documentId ? doc : d)));
        if (doc.status === 'complete' || doc.status === 'failed') return;
        interval = Math.min(interval * 1.5, POLL_MAX_MS);
      }
    } finally {
      this.polling.delete(documentId);
    }
  }

  protected async download(doc: ProjectDocument): Promise<void> {
    this.error.set(null);
    try {
      const { downloadUrl } = await firstValueFrom(this.api.fileDownloadUrl(this.projectId(), doc.documentId));
      window.open(downloadUrl, '_blank', 'noopener,noreferrer');
    } catch (err) {
      this.error.set(projectErrorMessage(err, `${doc.filename} could not be downloaded.`));
    }
  }

  protected async remove(doc: ProjectDocument): Promise<void> {
    const ref = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: 'Delete this file?',
        message: `“${doc.filename}” will be removed for everyone in the project, and the project’s agent will stop using it.`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        destructive: true,
      } as ConfirmationDialogData,
    });
    if (!(await firstValueFrom(ref.closed))) return;

    this.deletingId.set(doc.documentId);
    this.error.set(null);
    try {
      await firstValueFrom(this.api.deleteFile(this.projectId(), doc.documentId));
      this.polling.delete(doc.documentId);
      this.documents.update(list => list.filter(d => d.documentId !== doc.documentId));
    } catch (err) {
      this.error.set(projectErrorMessage(err, `${doc.filename} could not be deleted.`));
    } finally {
      this.deletingId.set(null);
    }
  }
}
