import { ChangeDetectionStrategy, Component, DestroyRef, computed, effect, inject, input, output, signal, untracked } from '@angular/core';
import { DatePipe } from '@angular/common';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowDownTray,
  heroArrowPath,
  heroArrowUpTray,
  heroDocumentText,
  heroGlobeAlt,
  heroLink,
  heroMagnifyingGlass,
  heroTrash,
} from '@ng-icons/heroicons/outline';
import { DocumentService, DocumentUploadError } from '../../assistants/services/document.service';
import { Document, DocumentStatus, KbUsage, PROCESSING_STATUSES } from '../../assistants/models/document.model';
import { KnowledgeTarget } from '../../assistants/models/knowledge-target.model';
import { FileSourceConnector } from '../../assistants/models/file-source.model';
import { CrawlJob } from '../../assistants/models/web-source.model';
import { SyncPolicy, SyncSourceType } from '../../assistants/models/sync-policy.model';
import { FileSourceService } from '../../assistants/services/file-source.service';
import { WebSourceService } from '../../assistants/services/web-source.service';
import { SyncPolicyService } from '../../assistants/services/sync-policy.service';
import {
  FileSourceBrowserDialogComponent,
  FileSourceBrowserDialogData,
} from '../../assistants/components/file-source-browser-dialog.component';
import { WebSourceDialogComponent, WebSourceDialogData } from '../../assistants/components/web-source-dialog.component';
import {
  ExtractedContentDialogComponent,
  ExtractedContentDialogData,
} from '../../assistants/components/extracted-content-dialog.component';
import {
  SyncIntervalSelection,
  SyncPolicyControlComponent,
} from '../../assistants/components/sync-policy-control.component';
import { SpinnerComponent } from '../../components/spinner/spinner.component';
import { UserConnectorsService } from '../../settings/connectors/services/user-connectors.service';
import { OAuthConsentService } from '../../services/oauth-consent/oauth-consent.service';
import { ToastService } from '../../services/toast/toast.service';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../components/confirmation-dialog/confirmation-dialog.component';
import { TooltipDirective } from '../../components/tooltip/tooltip.directive';
import { parseIso } from '../../utils/date';
import { personLabel } from '../../shared/utils/person';
import { Project, ProjectDocument } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';

/** The agent document pipeline's per-file limit (the knowledge-base section's too). */
const MAX_FILE_BYTES = 10 * 1024 * 1024;
const ACCEPT = '.pdf,.docx,.txt,.md,.html,.csv,.xls,.xlsx,.pptx';
const POLL_START_MS = 1000;
const POLL_MAX_MS = 10000;
const POLL_LIMIT_MS = 5 * 60 * 1000;
const CRAWL_WATCH_MS = 5000;

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
 *
 * Editors add files the same ways an agent's knowledge base does: upload, web
 * content (one page or a bounded crawl), and import from a connected file source
 * such as Google Drive. Crawled sites and imported files can keep themselves fresh
 * on a sync schedule. The crawl, import and sync dialogs and services are the
 * knowledge base's own, pointed at the project (`{ projectId }`).
 */
@Component({
  selector: 'app-project-files',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe, NgIcon, TooltipDirective, SpinnerComponent, SyncPolicyControlComponent],
  providers: [
    provideIcons({
      heroArrowDownTray,
      heroArrowPath,
      heroArrowUpTray,
      heroDocumentText,
      heroGlobeAlt,
      heroLink,
      heroMagnifyingGlass,
      heroTrash,
    }),
  ],
  template: `
    <div class="space-y-6">
      <section aria-labelledby="files-heading">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <h2 id="files-heading" class="text-sm/6 font-semibold text-gray-900 dark:text-white" [class.sr-only]="documents().length === 0">
            @if (documents().length) {
              {{ documents().length }} {{ documents().length === 1 ? 'file' : 'files' }}
            } @else {
              Files
            }
          </h2>
          @if (canEdit()) {
            <div class="ml-auto flex flex-wrap items-center justify-end gap-2" role="group" aria-label="Add to the project’s files">
              @for (source of fileSources(); track source.providerId) {
                @let busy = connectingProviderId() === source.providerId;
                <button
                  type="button"
                  (click)="openOrConnect(source)"
                  [disabled]="busy"
                  class="inline-flex items-center gap-2 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-60 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                >
                  @if (busy && connectPhase() === 'initiating') {
                    <app-spinner size="sm" label="Starting" />
                    Starting…
                  } @else if (busy && connectPhase() === 'awaiting') {
                    <ng-icon name="heroArrowPath" class="size-4 shrink-0 text-gray-400" aria-hidden="true" />
                    Awaiting consent
                  } @else {
                    @if (source.iconData) {
                      <img [src]="source.iconData" alt="" class="size-4 shrink-0 object-contain" />
                    } @else {
                      <ng-icon name="heroLink" class="size-4 shrink-0 text-gray-400" aria-hidden="true" />
                    }
                    @if (source.connected) {
                      {{ source.displayName }}
                      <span class="size-1.5 shrink-0 rounded-full bg-state-success-500" title="Connected"></span>
                    } @else {
                      Connect to {{ source.displayName }}
                    }
                  }
                </button>
              }
              <button
                type="button"
                (click)="openWebSourceDialog()"
                class="inline-flex items-center gap-2 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
              >
                <ng-icon name="heroGlobeAlt" class="size-4 shrink-0 text-gray-400" aria-hidden="true" />
                Add web content
              </button>
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
            </div>
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

        @if (webCrawls().length > 0) {
          <h3 class="mt-4 mb-2 text-sm/6 font-medium text-gray-700 dark:text-gray-300">Web sources</h3>
          <ul class="divide-y divide-gray-200 overflow-hidden rounded-2xl border border-gray-200 bg-white dark:divide-gray-700 dark:border-gray-700 dark:bg-gray-800">
            @for (crawl of webCrawls(); track crawl.crawlId) {
              <li class="flex items-start gap-3 px-4 py-3">
                <ng-icon name="heroGlobeAlt" class="mt-0.5 size-5 shrink-0 text-gray-500 dark:text-gray-400" aria-hidden="true" />
                <div class="min-w-0 flex-1">
                  <p class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">{{ crawl.rootUrl }}</p>
                  <p class="text-xs/5 text-gray-600 dark:text-gray-400">
                    {{ crawl.fetchedCount }} {{ crawl.fetchedCount === 1 ? 'page' : 'pages' }}
                    @if (crawl.status === 'running') {
                      · <span class="font-medium text-state-info-600 dark:text-state-info-400" role="status">Crawling…</span>
                    } @else if (crawl.status === 'failed') {
                      · <span class="font-medium text-state-danger-600 dark:text-state-danger-400">Crawl failed</span>
                    }
                  </p>
                  @if (crawl.status !== 'running') {
                    <app-sync-policy-control
                      class="mt-1.5 block"
                      [policy]="syncPolicyFor(crawl.crawlId)"
                      [busy]="isSyncBusy(crawl.crawlId)"
                      [sourceName]="crawl.rootUrl"
                      [lastSyncedAt]="crawl.completedAt ?? null"
                      (intervalSelected)="onSyncIntervalSelected('web_crawl', crawl.crawlId, $event)"
                      (runNow)="onSyncRunNow(crawl.crawlId)"
                      (pause)="onSyncPause(crawl.crawlId)"
                      (resume)="onSyncResume(crawl.crawlId)"
                    />
                  }
                </div>
                <!-- Enabled while running too: a live crawl is refused with a 409,
                     but one whose process died stays 'running' and must stay removable. -->
                <button
                  type="button"
                  (click)="removeWebSource(crawl)"
                  [appTooltip]="'Remove web source'"
                  appTooltipPosition="top"
                  [attr.aria-label]="'Remove ' + crawl.rootUrl"
                  class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-500 hover:bg-state-danger-50 hover:text-state-danger-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-600 dark:text-gray-400 dark:hover:bg-state-danger-900/20 dark:hover:text-state-danger-400"
                >
                  <ng-icon name="heroTrash" class="size-4" aria-hidden="true" />
                </button>
              </li>
            }
          </ul>
        }

        @if (loading() && documents().length === 0) {
          <div class="mt-4 h-24 animate-pulse rounded-2xl bg-gray-100 dark:bg-gray-700" aria-busy="true"></div>
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
                <ng-icon [name]="isWebPage(doc) ? 'heroGlobeAlt' : 'heroDocumentText'" class="size-5 shrink-0 text-gray-500 dark:text-gray-400" aria-hidden="true" />
                <div class="min-w-0 flex-1">
                  <p class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">{{ doc.filename }}</p>
                  <p class="text-xs/5 text-gray-600 dark:text-gray-400">
                    Added by <span [attr.title]="doc.addedByName ? doc.addedByEmail : null">{{ personLabel(doc.addedByName, doc.addedByEmail) || 'Unknown' }}</span> · {{ created(doc) | date: 'mediumDate' }} · {{ size(doc) }}
                  </p>
                  @if (doc.status === 'failed' && doc.errorMessage) {
                    <p class="text-xs/5 text-state-danger-600 dark:text-state-danger-400">{{ doc.errorMessage }}</p>
                  }
                  @if (canEdit() && isImported(doc)) {
                    <app-sync-policy-control
                      class="mt-1.5 block"
                      [policy]="syncPolicyFor(doc.documentId)"
                      [busy]="isSyncBusy(doc.documentId)"
                      [sourceName]="doc.filename"
                      [lastSyncedAt]="doc.lastSyncedAt ?? null"
                      [reconnectLabel]="providerName(doc)"
                      (intervalSelected)="onSyncIntervalSelected('drive_file', doc.documentId, $event)"
                      (runNow)="onSyncRunNow(doc.documentId)"
                      (pause)="onSyncPause(doc.documentId)"
                      (resume)="onSyncResume(doc.documentId)"
                      (reconnect)="onSyncReconnect(doc.documentId)"
                    />
                  }
                </div>
                <span
                  class="shrink-0 text-xs/5 font-medium"
                  [class]="statusClass(doc.status)"
                >{{ statusLabels[doc.status] }}</span>
                @if (canEdit() && doc.status === 'complete') {
                  <button
                    type="button"
                    (click)="viewExtractedContent(doc)"
                    [appTooltip]="'View extracted content'"
                    appTooltipPosition="top"
                    [attr.aria-label]="'View the content extracted from ' + doc.filename"
                    class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-500 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                  >
                    <ng-icon name="heroMagnifyingGlass" class="size-4" aria-hidden="true" />
                  </button>
                }
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
  private fileSourceService = inject(FileSourceService);
  private webSourceService = inject(WebSourceService);
  private syncPolicyService = inject(SyncPolicyService);
  private connectorsService = inject(UserConnectorsService);
  private consentService = inject(OAuthConsentService);
  private toast = inject(ToastService);
  private dialog = inject(Dialog);

  protected readonly personLabel = personLabel;

  readonly project = input.required<Project>();
  /** The number of files listed, whenever it changes (after a load, upload or delete). */
  readonly countChange = output<number>();

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

  // Editors only: the connectors they can import from, the project's web
  // sources, and the sync schedules on both (every route behind them is editor-only).
  protected readonly fileSources = signal<FileSourceConnector[]>([]);
  /** The connector whose consent popup is opening or open from its button. */
  protected readonly connectingProviderId = signal<string | null>(null);
  protected readonly connectPhase = signal<'initiating' | 'awaiting' | null>(null);
  protected readonly webCrawls = signal<CrawlJob[]>([]);
  protected readonly syncPolicies = signal<SyncPolicy[]>([]);
  /** Sources (document or crawl ids) with a sync change in flight. */
  private readonly syncBusy = signal<ReadonlySet<string>>(new Set());
  private readonly policiesBySource = computed(() => new Map(this.syncPolicies().map(p => [p.sourceRef, p])));
  /** The connector a sync control's "Reconnect" opened consent for, and that row. */
  private readonly reconnectingProviderId = signal<string | null>(null);
  private reconnectSourceRef: string | null = null;
  private reconnectAwaiting = false;
  private crawlWatcher: ReturnType<typeof setInterval> | null = null;
  /** The project whose editor-only sources have been loaded, so they load once per project. */
  private sourcesLoadedFor: string | null = null;

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
  private readonly target = computed<KnowledgeTarget>(() => ({ projectId: this.projectId() }));
  /** Documents being polled to a terminal status; cleared on project change and destroy. */
  private polling = new Set<string>();
  private generation = 0;

  constructor() {
    effect(() => {
      const id = this.projectId();
      untracked(() => {
        this.generation++;
        this.polling.clear();
        this.stopCrawlWatcher();
        this.documents.set([]);
        this.notice.set(null);
        this.upload.set(null);
        this.loaded.set(false);
        this.canEdit.set(false);
        this.fileSources.set([]);
        this.webCrawls.set([]);
        this.syncPolicies.set([]);
        this.sourcesLoadedFor = null;
        void this.load(id, null);
      });
    });
    inject(DestroyRef).onDestroy(() => {
      this.generation++;
      this.polling.clear();
      this.stopCrawlWatcher();
    });
    // Connectors, web sources and sync schedules, once the server says this person edits.
    effect(() => {
      const id = this.projectId();
      if (!this.canEdit() || this.sourcesLoadedFor === id) return;
      this.sourcesLoadedFor = id;
      untracked(() => {
        void this.loadFileSources();
        void this.loadSyncData();
      });
    });
    // A consent popup opened from a connector button or a sync control's "Reconnect".
    effect(() => {
      const completion = this.consentService.completion();
      if (!completion?.providerId) return;
      untracked(() => this.onConsentCompleted(completion.providerId!, completion.status === 'success', completion.error));
    });
    // Closing the popup without finishing drops the provider from the in-flight set.
    effect(() => {
      const inFlight = this.consentService.inFlightProviders();
      untracked(() => {
        const connecting = this.connectingProviderId();
        if (connecting && this.connectPhase() === 'awaiting' && !inFlight.has(connecting)) this.resetConnect();
        const reconnecting = this.reconnectingProviderId();
        if (reconnecting && this.reconnectAwaiting && !inFlight.has(reconnecting)) this.finishReconnect();
      });
    });
    // Reported only once a list has loaded, so an opener's count never flashes to 0.
    effect(() => {
      const n = this.documents().length;
      if (this.loaded()) this.countChange.emit(n);
    });
  }

  protected created(doc: ProjectDocument): Date {
    return parseIso(doc.createdAt);
  }

  protected size(doc: ProjectDocument): string {
    return formatBytes(doc.sizeBytes);
  }

  /** A page a web crawl added; its crawl, not the page, is what syncs. */
  protected isWebPage(doc: Document): boolean {
    return doc.sourceConnectorId === 'web';
  }

  /** Imported from a connected file source, so there is an original to sync from. */
  protected isImported(doc: Document): boolean {
    return !!doc.sourceFileId && !!doc.sourceConnectorId && doc.sourceConnectorId !== 'web';
  }

  /** The file source a document came from, by name ("" when it is no longer offered). */
  protected providerName(doc: Document): string {
    return this.fileSources().find(s => s.providerId === doc.sourceConnectorId)?.displayName ?? '';
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
      // The server deletes a file's sync schedule with it.
      this.dropPolicyFor(doc.documentId);
    } catch (err) {
      this.error.set(projectErrorMessage(err, `${doc.filename} could not be deleted.`));
    } finally {
      this.deletingId.set(null);
    }
  }

  protected viewExtractedContent(doc: ProjectDocument): void {
    this.dialog.open<void, ExtractedContentDialogData>(ExtractedContentDialogComponent, {
      data: { target: this.target(), documentId: doc.documentId, filename: doc.filename },
      hasBackdrop: false,
    });
  }

  // ---- web content ------------------------------------------------------

  protected async openWebSourceDialog(): Promise<void> {
    const generation = this.generation;
    const ref = this.dialog.open<Document[] | undefined, WebSourceDialogData>(WebSourceDialogComponent, {
      data: { target: this.target() },
      hasBackdrop: false,
    });
    const added = await firstValueFrom(ref.closed);
    if (!added?.length || generation !== this.generation) return;
    this.toast.success('Crawling web content…');
    await Promise.all([this.load(this.projectId(), null), this.loadCrawls()]);
    this.startCrawlWatcher();
  }

  /** While a crawl runs, refresh the files every few seconds so its pages appear as they land. */
  private startCrawlWatcher(): void {
    if (this.crawlWatcher !== null) return;
    const generation = this.generation;
    this.crawlWatcher = setInterval(async () => {
      if (generation !== this.generation) return;
      try {
        const running = await this.webSourceService.listActiveCrawls(this.target());
        if (generation !== this.generation) return;
        if (running.length === 0) {
          this.stopCrawlWatcher();
          await this.loadCrawls();
        }
        await this.load(this.projectId(), null);
      } catch {
        // A blip; the next tick tries again.
      }
    }, CRAWL_WATCH_MS);
  }

  private stopCrawlWatcher(): void {
    if (this.crawlWatcher !== null) {
      clearInterval(this.crawlWatcher);
      this.crawlWatcher = null;
    }
  }

  /** Remove a crawled site: its pages, and any sync schedule on it. */
  protected async removeWebSource(crawl: CrawlJob): Promise<void> {
    const pages = `${crawl.fetchedCount} ${crawl.fetchedCount === 1 ? 'page' : 'pages'}`;
    const ref = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: 'Remove this web source?',
        message: `${crawl.rootUrl} and the ${pages} it added will be removed for everyone in the project, along with any sync schedule on it.`,
        confirmText: 'Remove',
        cancelText: 'Cancel',
        destructive: true,
      } as ConfirmationDialogData,
    });
    if (!(await firstValueFrom(ref.closed))) return;

    const isPage = (d: Document) => d.sourceConnectorId === 'web' && !!d.sourceFileId?.startsWith(crawl.rootUrl);
    try {
      await this.webSourceService.deleteCrawl(this.target(), crawl.crawlId);
      this.webCrawls.update(list => list.filter(c => c.crawlId !== crawl.crawlId));
      for (const d of this.documents()) if (isPage(d)) this.polling.delete(d.documentId);
      this.documents.update(list => list.filter(d => !isPage(d)));
      this.dropPolicyFor(crawl.crawlId);
      this.toast.success('Web source removed.');
    } catch (err) {
      this.toast.error(err instanceof Error && err.message ? err.message : 'The web source could not be removed.');
    }
  }

  // ---- file sources (Google Drive, …) ------------------------------------

  /** Optional: with no connector set up (or on any error) there are simply no buttons. */
  private async loadFileSources(): Promise<void> {
    const generation = this.generation;
    try {
      const sources = await this.fileSourceService.listFileSources();
      if (generation === this.generation) this.fileSources.set(sources);
    } catch {
      if (generation === this.generation) this.fileSources.set([]);
    }
  }

  /** Browse a connected source; otherwise connect it first, and browse once consent completes. */
  protected async openOrConnect(source: FileSourceConnector): Promise<void> {
    if (source.connected) {
      await this.openFileSourceBrowser(source);
      return;
    }
    this.connectingProviderId.set(source.providerId);
    this.connectPhase.set('initiating');
    try {
      const result = await this.connectorsService.initiateConsent(source.providerId);
      if (result.connected) {
        this.resetConnect();
        await this.afterConnect(source.providerId);
        return;
      }
      if (!result.authorizationUrl) {
        this.resetConnect();
        this.toast.error('Unexpected response from the server.');
        return;
      }
      this.consentService.requestConsent(source.providerId, result.authorizationUrl);
      void this.consentService.openConsentPopup(source.providerId);
      this.connectPhase.set('awaiting');
    } catch (err) {
      this.resetConnect();
      this.toast.error(err instanceof Error && err.message ? err.message : 'Could not start the connect flow.');
    }
  }

  private resetConnect(): void {
    this.connectingProviderId.set(null);
    this.connectPhase.set(null);
  }

  private async afterConnect(providerId: string): Promise<void> {
    await this.loadFileSources();
    const source = this.fileSources().find(s => s.providerId === providerId);
    if (source?.connected) await this.openFileSourceBrowser(source);
  }

  private async openFileSourceBrowser(connector: FileSourceConnector): Promise<void> {
    const generation = this.generation;
    const ref = this.dialog.open<Document[] | undefined, FileSourceBrowserDialogData>(FileSourceBrowserDialogComponent, {
      data: { target: this.target(), connector },
      hasBackdrop: false,
    });
    const imported = await firstValueFrom(ref.closed);
    if (!imported?.length || generation !== this.generation) return;
    this.toast.success(`Importing ${imported.length} ${imported.length === 1 ? 'file' : 'files'}…`);
    await this.load(this.projectId(), null);
  }

  private onConsentCompleted(providerId: string, ok: boolean, error?: string | null): void {
    if (providerId === this.reconnectingProviderId()) {
      // The server resumes the schedules a fresh consent was waiting on.
      this.consentService.acknowledgeCompletion();
      this.finishReconnect();
      if (ok) {
        this.toast.success('Reconnected — sync will resume automatically.');
        void this.loadSyncData();
      } else {
        this.toast.error(error ?? 'Could not reconnect the content source.');
      }
      return;
    }
    if (providerId !== this.connectingProviderId()) return;
    this.consentService.acknowledgeCompletion();
    this.resetConnect();
    if (ok) void this.afterConnect(providerId);
    else this.toast.error(error ?? 'Could not connect the file source.');
  }

  // ---- sync schedules ---------------------------------------------------

  /** Schedules and web sources, best effort: the files list stands on its own without them. */
  private async loadSyncData(): Promise<void> {
    const generation = this.generation;
    const [policies] = await Promise.allSettled([this.syncPolicyService.listPolicies(this.target()), this.loadCrawls()]);
    if (generation !== this.generation) return;
    if (policies.status === 'fulfilled') this.syncPolicies.set(policies.value);
    if (this.webCrawls().some(c => c.status === 'running')) this.startCrawlWatcher();
  }

  private async loadCrawls(): Promise<void> {
    const generation = this.generation;
    try {
      const crawls = await this.webSourceService.listCrawls(this.target());
      if (generation === this.generation) this.webCrawls.set(crawls);
    } catch {
      // Keep what is shown; the next change refreshes it.
    }
  }

  protected syncPolicyFor(sourceRef: string): SyncPolicy | null {
    return this.policiesBySource().get(sourceRef) ?? null;
  }

  protected isSyncBusy(sourceRef: string): boolean {
    return this.syncBusy().has(sourceRef);
  }

  protected async onSyncIntervalSelected(
    sourceType: SyncSourceType,
    sourceRef: string,
    selection: SyncIntervalSelection,
  ): Promise<void> {
    const existing = this.syncPolicyFor(sourceRef);
    await this.syncChange(sourceRef, 'Could not update sync settings.', async () => {
      if (selection === 'manual') {
        if (existing) {
          await this.syncPolicyService.deletePolicy(this.target(), existing.policyId);
          this.dropPolicyFor(sourceRef);
        }
      } else if (existing) {
        this.upsertPolicy(await this.syncPolicyService.updatePolicy(this.target(), existing.policyId, { interval: selection }));
      } else {
        this.upsertPolicy(await this.syncPolicyService.createPolicy(this.target(), { sourceType, sourceRef, interval: selection }));
      }
    });
  }

  protected onSyncPause(sourceRef: string): Promise<void> {
    return this.setSyncState(sourceRef, 'paused_user', 'Could not pause sync.');
  }

  protected onSyncResume(sourceRef: string): Promise<void> {
    return this.setSyncState(sourceRef, 'active', 'Could not resume sync.');
  }

  private async setSyncState(sourceRef: string, state: 'active' | 'paused_user', failure: string): Promise<void> {
    const existing = this.syncPolicyFor(sourceRef);
    if (!existing) return;
    await this.syncChange(sourceRef, failure, async () => {
      this.upsertPolicy(await this.syncPolicyService.updatePolicy(this.target(), existing.policyId, { state }));
    });
  }

  protected async onSyncRunNow(sourceRef: string): Promise<void> {
    const existing = this.syncPolicyFor(sourceRef);
    if (!existing) return;
    await this.syncChange(
      sourceRef,
      'Could not request a sync.',
      async () => {
        this.upsertPolicy(await this.syncPolicyService.runNow(this.target(), existing.policyId));
        this.toast.success('Sync requested — it will run within about 15 minutes.');
      },
      // A cooldown or an inactive schedule says why in the server's own words; nothing is stale.
      false,
    );
  }

  /** A schedule paused because its connection lapsed resumes after a fresh consent. */
  protected async onSyncReconnect(sourceRef: string): Promise<void> {
    const providerId = this.documents().find(d => d.documentId === sourceRef)?.sourceConnectorId;
    if (!providerId || providerId === 'web') return;
    this.reconnectingProviderId.set(providerId);
    this.reconnectSourceRef = sourceRef;
    this.setBusy(sourceRef, true);
    try {
      const result = await this.connectorsService.initiateConsent(providerId);
      if (result.connected) {
        // The token looks fine to the vault, so no popup opens and nothing resumes on its own.
        this.finishReconnect();
        await this.loadSyncData();
        if (this.syncPolicyFor(sourceRef)?.state === 'paused_reauth') {
          this.toast.error(
            'The connection still needs to be re-authorized. Disconnect and reconnect it under Settings → Connectors, then sync will resume.',
          );
        } else {
          this.toast.success('Reconnected — sync will resume automatically.');
        }
        return;
      }
      if (!result.authorizationUrl) {
        this.finishReconnect();
        this.toast.error('Unexpected response from the server.');
        return;
      }
      this.consentService.requestConsent(providerId, result.authorizationUrl);
      void this.consentService.openConsentPopup(providerId);
      this.reconnectAwaiting = true;
    } catch (err) {
      this.finishReconnect();
      this.toast.error(err instanceof Error && err.message ? err.message : 'Could not start the reconnect flow.');
    }
  }

  private finishReconnect(): void {
    this.reconnectingProviderId.set(null);
    this.reconnectAwaiting = false;
    if (this.reconnectSourceRef) this.setBusy(this.reconnectSourceRef, false);
    this.reconnectSourceRef = null;
  }

  /** Run one schedule change with its row busy; on failure, say so and (by default) re-read the schedules. */
  private async syncChange(sourceRef: string, failure: string, change: () => Promise<void>, reloadOnError = true): Promise<void> {
    this.setBusy(sourceRef, true);
    try {
      await change();
    } catch (err) {
      this.toast.error(err instanceof Error && err.message ? err.message : failure);
      if (reloadOnError) void this.loadSyncData();
    } finally {
      this.setBusy(sourceRef, false);
    }
  }

  private setBusy(sourceRef: string, busy: boolean): void {
    this.syncBusy.update(set => {
      const next = new Set(set);
      if (busy) next.add(sourceRef);
      else next.delete(sourceRef);
      return next;
    });
  }

  private upsertPolicy(policy: SyncPolicy): void {
    this.syncPolicies.update(list =>
      list.some(p => p.policyId === policy.policyId) ? list.map(p => (p.policyId === policy.policyId ? policy : p)) : [...list, policy],
    );
  }

  private dropPolicyFor(sourceRef: string): void {
    this.syncPolicies.update(list => list.filter(p => p.sourceRef !== sourceRef));
  }
}
