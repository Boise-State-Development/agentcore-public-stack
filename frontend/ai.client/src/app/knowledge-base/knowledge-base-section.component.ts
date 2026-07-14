import {
  Component,
  ChangeDetectionStrategy,
  inject,
  input,
  signal,
  computed,
  effect,
  OnDestroy,
} from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowDownTray,
  heroArrowPath,
  heroGlobeAlt,
  heroLink,
  heroPlus,
  heroTrash,
} from '@ng-icons/heroicons/outline';
import { Dialog } from '@angular/cdk/dialog';
import { DocumentService, DocumentUploadError } from '../assistants/services/document.service';
import {
  Document,
  PROCESSING_STATUSES,
  STALE_DOCUMENT_THRESHOLD_MS,
} from '../assistants/models/document.model';
import {
  FileSourceBrowserDialogComponent,
  FileSourceBrowserDialogData,
} from '../assistants/components/file-source-browser-dialog.component';
import {
  WebSourceDialogComponent,
  WebSourceDialogData,
} from '../assistants/components/web-source-dialog.component';
import { FileSourceService } from '../assistants/services/file-source.service';
import { WebSourceService } from '../assistants/services/web-source.service';
import { SyncPolicyService } from '../assistants/services/sync-policy.service';
import { FileSourceConnector } from '../assistants/models/file-source.model';
import { CrawlJob } from '../assistants/models/web-source.model';
import { SyncPolicy, SyncSourceType } from '../assistants/models/sync-policy.model';
import {
  SyncPolicyControlComponent,
  SyncIntervalSelection,
} from '../assistants/components/sync-policy-control.component';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../components/confirmation-dialog';
import { UserConnectorsService } from '../settings/connectors/services/user-connectors.service';
import { OAuthConsentService } from '../services/oauth-consent/oauth-consent.service';
import { ToastService } from '../services/toast/toast.service';

/**
 * The reusable "Knowledge base" authoring section — device upload, web-crawl
 * dialog, connector import (with OAuth consent), upload progress, document +
 * web-source lists, delete, and per-source sync-policy controls. Extracted from
 * the assistant editor so both the assistant form and the Agent Designer share
 * one implementation (the document pipeline already keys on the record id, and
 * `agentId == assistantId`, so the same `/assistants/{id}/documents` surface
 * backs both).
 *
 * The parent owns record identity: in create mode the record doesn't exist yet,
 * so the first upload/import/crawl mints one via the {@link createDraft}
 * callback. The callback is entity-specific (assistant vs. agent), updates the
 * parent's own state, and returns the new id — which this component then uses
 * for every subsequent call.
 */
@Component({
  selector: 'app-knowledge-base-section',
  templateUrl: './knowledge-base-section.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, SyncPolicyControlComponent],
  providers: [
    provideIcons({
      heroArrowDownTray,
      heroArrowPath,
      heroGlobeAlt,
      heroLink,
      heroPlus,
      heroTrash,
    }),
  ],
})
export class KnowledgeBaseSectionComponent implements OnDestroy {
  private documentService = inject(DocumentService);
  private fileSourceService = inject(FileSourceService);
  private webSourceService = inject(WebSourceService);
  private syncPolicyService = inject(SyncPolicyService);
  private readonly connectorsService = inject(UserConnectorsService);
  private readonly consentService = inject(OAuthConsentService);
  private readonly dialog = inject(Dialog);
  private readonly toast = inject(ToastService);

  // ── Inputs ────────────────────────────────────────────────────────────

  /** The parent record's id (assistant/agent). `null` in create mode. */
  readonly entityId = input<string | null>(null);
  /** The requesting user's permission — gates the whole sync surface. */
  readonly userPermission = input<'owner' | 'editor' | 'viewer'>('owner');
  /**
   * Whether {@link userPermission} reflects a resolved value. Edit-mode parents
   * pass `false` until they've loaded the record, so we don't issue the
   * sync-policy list call with the default 'owner' guess (a viewer would 403).
   * Create-mode parents leave it `true` (the user is implicitly the owner).
   */
  readonly permissionResolved = input<boolean>(true);
  /**
   * Mint a persisted record so uploads have a parent to attach to, returning
   * its id. Called on the first content-adding action in create mode. The
   * parent implementation also patches its own form/state from the draft.
   */
  readonly createDraft = input.required<() => Promise<string>>();

  /**
   * The effective record id. Mirrors {@link entityId} (once non-null) and is
   * also set the moment a draft is minted, so uploads can proceed immediately
   * without waiting for the input to propagate back through change detection.
   */
  private readonly id = signal<string | null>(null);
  /** Guards so each id hydrates its document/sync lists at most once. */
  private hydratedForId: string | null = null;
  private syncHydratedForId: string | null = null;

  readonly mode = computed<'create' | 'edit'>(() => (this.id() ? 'edit' : 'create'));

  readonly uploadedDocuments = signal<Document[]>([]);
  readonly isLoadingDocuments = signal<boolean>(false);
  readonly currentUpload = signal<{
    file: File;
    progress: number;
    status: 'uploading' | 'complete' | 'error';
    error?: string;
  } | null>(null);
  readonly pollingDocuments = signal<Set<string>>(new Set());

  /** Connectors the user can import documents from, surfaced as buttons. */
  readonly fileSources = signal<FileSourceConnector[]>([]);
  /**
   * True while the section is fetching the connector catalog for the first
   * time. Drives the inline skeleton chips so the connector buttons fade in
   * rather than popping into existence after a network round-trip.
   */
  readonly fileSourcesLoading = signal<boolean>(true);

  /** Provider whose consent popup is in flight from a connector button. */
  readonly connectingProviderId = signal<string | null>(null);
  readonly connectPhase = signal<'initiating' | 'awaiting' | null>(null);

  /**
   * True while a web crawl is running for this record. Drives a small
   * "crawling…" badge so the user knows pages will keep appearing for a while
   * after the dialog closes.
   */
  readonly webCrawlActive = signal<boolean>(false);
  private crawlWatcherHandle: ReturnType<typeof setInterval> | null = null;

  // ── KB sync policies ────────────────────────────────────────────────────

  /** Sync policies covering this record's sources. Loaded for owners/editors only. */
  readonly syncPolicies = signal<SyncPolicy[]>([]);
  /** All crawl jobs (any status) — completed crawls are the syncable web sources. */
  readonly webCrawls = signal<CrawlJob[]>([]);
  /**
   * True while the crawl catalog is doing its initial fetch. Mirrors
   * {@link isLoadingDocuments} so both knowledge lists share one loading gate
   * and reveal together.
   */
  readonly isLoadingCrawls = signal<boolean>(false);
  /** Source refs (document/crawl ids) with a sync mutation in flight. */
  readonly syncBusySourceRefs = signal<Set<string>>(new Set());
  /** Provider whose consent popup was opened from a "Reconnect" affordance. */
  readonly reconnectingProviderId = signal<string | null>(null);

  /**
   * Sync controls are owner/editor-only: the backend edit-gates the whole
   * sync-policy surface, so viewers never see the controls (and we never issue
   * the list call that would 403 for them).
   */
  readonly canManageSync = computed(
    () => this.mode() === 'edit' && this.userPermission() !== 'viewer',
  );

  private readonly policiesBySourceRef = computed(
    () => new Map(this.syncPolicies().map((p) => [p.sourceRef, p])),
  );

  /**
   * Crawls that can carry a sync policy. A `running` crawl is excluded — its
   * page set is still forming — but it still renders in the web-sources list
   * with a progress note.
   */
  readonly syncableCrawlStatuses: ReadonlySet<CrawlJob['status']> = new Set([
    'complete',
    'failed',
  ]);

  /**
   * True once at least one document exists, is uploading, or is still loading.
   * Drives swapping the full drop zone for a compact "Add files" control.
   */
  readonly hasDocuments = computed(
    () =>
      this.uploadedDocuments().length > 0 ||
      this.currentUpload() !== null ||
      this.isLoadingDocuments() ||
      this.isLoadingCrawls(),
  );

  /**
   * Single initial-load gate for the two knowledge lists (Web sources +
   * Uploaded Documents). While true a skeleton stands in for both.
   */
  readonly isLoadingKnowledge = computed(
    () => this.isLoadingDocuments() || this.isLoadingCrawls(),
  );

  /**
   * Varied bar widths (percent) for the knowledge skeleton rows, so the
   * placeholder reads as content rather than a repeating pattern.
   */
  readonly skeletonRows: ReadonlyArray<{ title: number; meta: number }> = [
    { title: 58, meta: 34 },
    { title: 72, meta: 42 },
    { title: 46, meta: 28 },
  ];

  constructor() {
    // Mirror the id input into the effective id (once non-null). `set` is a
    // no-op when the value is unchanged, so this never loops.
    effect(() => {
      const e = this.entityId();
      if (e) {
        this.id.set(e);
      }
    });

    // Hydrate the document + web-source lists when the id first becomes known
    // (edit mode). A draft minted mid-session via ensureId marks itself
    // hydrated first, so this early-returns for it — a brand-new record has
    // nothing to load and would only flash a skeleton. Both loading gates are
    // raised so the lists reveal together; loadDocuments/loadSyncData clear
    // them in their finally blocks.
    effect(() => {
      const id = this.id();
      if (!id || id === this.hydratedForId) {
        return;
      }
      this.hydratedForId = id;
      this.isLoadingDocuments.set(true);
      this.isLoadingCrawls.set(true);
      void this.loadDocuments();
    });

    // Load sync data once the id is known AND the permission is resolved — the
    // sync surface is edit-gated and we must not issue the list call with the
    // default 'owner' guess (a viewer would 403). loadSyncData releases the
    // crawl-loading gate, including on the viewer early-return.
    effect(() => {
      const id = this.id();
      if (!id || !this.permissionResolved() || id === this.syncHydratedForId) {
        return;
      }
      this.syncHydratedForId = id;
      void this.loadSyncData();
    });

    // Resolve the OAuth consent popup for a connector kicked off from a section
    // button. Mirrors the file-source browser dialog's effect so the section
    // can drive the flow without opening the modal first.
    effect(() => {
      const completion = this.consentService.completion();
      if (!completion || !completion.providerId) {
        return;
      }
      // A consent kicked off from a sync-control "Reconnect" affordance: a
      // fresh consent auto-resumes paused_reauth policies server-side, so all
      // that's left here is to refetch and confirm.
      if (completion.providerId === this.reconnectingProviderId()) {
        this.consentService.acknowledgeCompletion();
        this.finishReconnect();
        if (completion.status === 'success') {
          this.toast.success('Reconnected — sync will resume automatically.');
          void this.loadSyncData();
        } else {
          this.toast.error(completion.error ?? 'Could not reconnect the content source.');
        }
        return;
      }
      const connecting = this.connectingProviderId();
      if (completion.providerId !== connecting) {
        return;
      }
      this.consentService.acknowledgeCompletion();
      this.connectingProviderId.set(null);
      this.connectPhase.set(null);
      if (completion.status === 'success') {
        void this.afterConnect(connecting);
      } else {
        this.toast.error(completion.error ?? 'Could not connect the file source.');
      }
    });

    // If the user closes the popup without finishing, the consent service drops
    // the provider from `inFlightProviders` — reset the button state.
    effect(() => {
      const inFlight = this.consentService.inFlightProviders();
      const connecting = this.connectingProviderId();
      if (connecting && this.connectPhase() === 'awaiting' && !inFlight.has(connecting)) {
        this.connectingProviderId.set(null);
        this.connectPhase.set(null);
      }
      const reconnecting = this.reconnectingProviderId();
      if (reconnecting && this.reconnectAwaiting && !inFlight.has(reconnecting)) {
        this.finishReconnect();
      }
    });

    // Load the connectors the user can import documents from (create or edit).
    void this.loadFileSources();
  }

  ngOnDestroy(): void {
    this.stopCrawlWatcher();
  }

  /**
   * Ensure a persisted record exists so documents have a parent to attach to.
   * In create mode the parent has no record yet, so a draft is minted via the
   * {@link createDraft} callback (which also patches the parent's state) and
   * its id is adopted locally. Returns the id; throws if draft creation fails.
   */
  private async ensureId(): Promise<string> {
    const existing = this.id();
    if (existing) {
      return existing;
    }
    const minted = await this.createDraft()();
    // A brand-new draft has nothing to hydrate — mark it done so the hydrate
    // effects skip it (no skeleton flash, no eager sync list on an empty record).
    this.hydratedForId = minted;
    this.syncHydratedForId = minted;
    this.id.set(minted);
    return minted;
  }

  async onFileSelected(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];

    if (!file) {
      return;
    }

    // Validate file size (10MB max)
    const maxSizeBytes = 10 * 1024 * 1024; // 10MB
    if (file.size > maxSizeBytes) {
      this.currentUpload.set({
        file,
        progress: 0,
        status: 'error',
        error: `File size exceeds 10MB limit. File size: ${this.formatBytes(file.size)}`,
      });
      input.value = '';
      return;
    }

    // Ensure we have a record id (create draft if in create mode)
    let recordId: string;
    try {
      recordId = await this.ensureId();
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : 'Failed to create record';
      this.currentUpload.set({
        file,
        progress: 0,
        status: 'error',
        error: errorMessage,
      });
      input.value = '';
      return;
    }

    // Upload the document
    await this.uploadDocument(file, recordId);

    // Clear the input to allow re-selecting the same file
    input.value = '';
  }

  /**
   * Load the connectors the user can import documents from. The feature is
   * optional — on any error (not configured, no access) just surface no
   * connector buttons rather than blocking the editor.
   */
  private async loadFileSources(): Promise<void> {
    this.fileSourcesLoading.set(true);
    try {
      this.fileSources.set(await this.fileSourceService.listFileSources());
    } catch {
      this.fileSources.set([]);
    } finally {
      this.fileSourcesLoading.set(false);
    }
  }

  /**
   * Click handler for a connector button: browse when the source is already
   * connected; otherwise kick off the OAuth consent flow in place so the user
   * doesn't have to open the modal just to connect.
   */
  async openOrConnect(source: FileSourceConnector): Promise<void> {
    if (source.connected) {
      await this.openFileSourceBrowser(source);
      return;
    }
    await this.connectFileSource(source);
  }

  /**
   * Start the OAuth consent popup for a not-yet-connected file source. On
   * success the browser modal opens automatically — see the completion effect
   * → `afterConnect`. Mirrors the dialog's `connect()` path.
   */
  private async connectFileSource(source: FileSourceConnector): Promise<void> {
    this.connectingProviderId.set(source.providerId);
    this.connectPhase.set('initiating');
    try {
      const result = await this.connectorsService.initiateConsent(source.providerId);
      if (result.connected) {
        // Already connected upstream — skip the popup and go straight to browse.
        this.connectingProviderId.set(null);
        this.connectPhase.set(null);
        await this.afterConnect(source.providerId);
        return;
      }
      if (!result.authorizationUrl) {
        this.connectingProviderId.set(null);
        this.connectPhase.set(null);
        this.toast.error('Unexpected response from the server.');
        return;
      }
      this.consentService.requestConsent(source.providerId, result.authorizationUrl);
      void this.consentService.openConsentPopup(source.providerId);
      this.connectPhase.set('awaiting');
    } catch (error) {
      this.connectingProviderId.set(null);
      this.connectPhase.set(null);
      const message =
        error instanceof Error ? error.message : 'Could not start the connect flow.';
      this.toast.error(message);
    }
  }

  /**
   * After a successful consent, refresh the file-source list (so the connector
   * now reports `connected: true`) and open the browser modal straight into it
   * so the user can pick files without a second click.
   */
  private async afterConnect(providerId: string): Promise<void> {
    await this.loadFileSources();
    const updated = this.fileSources().find((s) => s.providerId === providerId);
    if (updated?.connected) {
      await this.openFileSourceBrowser(updated);
    }
  }

  /**
   * Open the file-source browser so the user can import documents from a
   * connector (Google Drive, etc.). When `connector` is given the browser
   * opens straight into it. Ensures a draft record exists first — imported
   * documents need a parent. On close, any imported documents are merged into
   * the list and polled like a device upload.
   */
  async openFileSourceBrowser(connector?: FileSourceConnector): Promise<void> {
    let recordId: string;
    try {
      recordId = await this.ensureId();
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to create record';
      this.toast.error(message);
      return;
    }

    const dialogRef = this.dialog.open<Document[] | undefined, FileSourceBrowserDialogData>(
      FileSourceBrowserDialogComponent,
      {
        data: { assistantId: recordId, connector },
        hasBackdrop: false,
      },
    );

    const imported = await firstValueFrom(dialogRef.closed);
    if (imported && imported.length > 0) {
      this.toast.success(
        `Importing ${imported.length} file${imported.length === 1 ? '' : 's'}…`,
      );
      // loadDocuments() picks up the new 'uploading' records and starts polling
      // them through to 'complete', exactly like a device upload.
      await this.loadDocuments();
    }
  }

  /**
   * Open the web-source dialog so the user can attach a URL (single page or a
   * bounded crawl). Mirrors {@link openFileSourceBrowser} — ensures a draft
   * record exists, opens the dialog, then on close merges the pre-created root
   * document into the list and starts the crawl watcher so additional pages
   * surface as the crawler discovers them.
   */
  async openWebSourceDialog(): Promise<void> {
    let recordId: string;
    try {
      recordId = await this.ensureId();
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to create record';
      this.toast.error(message);
      return;
    }

    const dialogRef = this.dialog.open<Document[] | undefined, WebSourceDialogData>(
      WebSourceDialogComponent,
      {
        data: { assistantId: recordId },
        hasBackdrop: false,
      },
    );

    const imported = await firstValueFrom(dialogRef.closed);
    if (imported && imported.length > 0) {
      this.toast.success('Crawling web content…');
      await this.loadDocuments();
      this.startCrawlWatcher();
      // Surface the new crawl in the web-sources list right away (it shows as
      // running until the watcher sees it finish).
      void this.loadSyncData();
    }
  }

  /**
   * Poll active crawls for this record every few seconds. While any are
   * `running` we surface newly-discovered pages via {@link discoverNewDocuments}
   * — an *incremental* merge that appends only the new rows. Stops itself once
   * the server reports no active crawls.
   */
  private startCrawlWatcher(): void {
    this.webCrawlActive.set(true);
    this.stopCrawlWatcher();
    const tick = async (): Promise<void> => {
      const recordId = this.id();
      if (!recordId) {
        this.stopCrawlWatcher();
        return;
      }
      try {
        const active = await this.webSourceService.listActiveCrawls(recordId);
        if (active.length === 0) {
          this.webCrawlActive.set(false);
          this.stopCrawlWatcher();
          // Catch any pages that completed in the gap between the previous tick
          // and the server reporting "no crawls running".
          await this.discoverNewDocuments();
          // The crawl just went terminal — refresh the web-sources list so its
          // row flips from "Crawling…" to a syncable source.
          void this.loadSyncData();
          return;
        }
        await this.discoverNewDocuments();
      } catch {
        // Network blip — keep polling; the watcher is non-critical.
      }
    };
    this.crawlWatcherHandle = setInterval(() => void tick(), 5000);
  }

  private stopCrawlWatcher(): void {
    if (this.crawlWatcherHandle !== null) {
      clearInterval(this.crawlWatcherHandle);
      this.crawlWatcherHandle = null;
    }
  }

  /**
   * Fetch the record's documents and append only the IDs we don't already have
   * to the local list — does NOT replace existing rows. Used by the crawl
   * watcher so each new page slides in when it appears.
   */
  private async discoverNewDocuments(): Promise<void> {
    const recordId = this.id();
    if (!recordId) {
      return;
    }
    try {
      const response = await this.documentService.listDocuments(recordId);
      const existing = new Set(this.uploadedDocuments().map((doc) => doc.documentId));
      const newDocs = response.documents.filter((doc) => !existing.has(doc.documentId));
      if (newDocs.length === 0) {
        return;
      }
      this.uploadedDocuments.update((docs) => [...docs, ...newDocs]);
      for (const doc of newDocs) {
        if (
          PROCESSING_STATUSES.includes(doc.status) &&
          !this.isDocumentStale(doc) &&
          !this.pollingDocuments().has(doc.documentId)
        ) {
          this.startPollingDocument(doc.documentId, recordId);
        }
      }
    } catch (error) {
      console.error('Error discovering new documents:', error);
    }
  }

  async uploadDocument(file: File, recordId: string): Promise<void> {
    // Set initial upload state
    this.currentUpload.set({
      file,
      progress: 0,
      status: 'uploading',
    });

    let documentId: string | undefined;

    try {
      // Step 1: Request presigned URL
      const uploadUrlResponse = await this.documentService.requestUploadUrl(recordId, file);
      documentId = uploadUrlResponse.documentId;

      // Step 2: Upload to S3 with progress tracking
      await this.documentService.uploadToS3(uploadUrlResponse.uploadUrl, file, (progress) => {
        this.currentUpload.update((current) => {
          if (!current) return current;
          return { ...current, progress };
        });
      });

      // Step 3: Mark upload as complete
      this.currentUpload.set({
        file,
        progress: 100,
        status: 'complete',
      });

      // Step 4: Reload documents list to get the new document
      await this.loadDocuments();

      // Step 5: Start polling for document processing status
      this.startPollingDocument(uploadUrlResponse.documentId, recordId);

      // Clear upload state after a short delay
      setTimeout(() => {
        this.currentUpload.set(null);
      }, 2000);
    } catch (error) {
      const errorMessage =
        error instanceof DocumentUploadError
          ? error.message
          : error instanceof Error
            ? error.message
            : 'Upload failed';

      this.currentUpload.set({
        file,
        progress: this.currentUpload()?.progress || 0,
        status: 'error',
        error: errorMessage,
      });

      // Report the failure to the backend so the DynamoDB record is marked as
      // 'failed' instead of stuck in 'uploading'. This prevents infinite
      // polling on page refresh.
      if (documentId) {
        const details =
          error instanceof DocumentUploadError ? JSON.stringify(error.details) : undefined;
        this.documentService.reportUploadFailure(recordId, documentId, errorMessage, details);
      }
    }
  }

  /**
   * Check if a document in a processing state is stale (updatedAt too old).
   * Matches the backend's 10-minute threshold so the frontend can skip polling
   * for documents that the backend will auto-fail on next fetch.
   */
  private isDocumentStale(doc: Document): boolean {
    try {
      const updatedAt = new Date(doc.updatedAt).getTime();
      return Date.now() - updatedAt > STALE_DOCUMENT_THRESHOLD_MS;
    } catch {
      return true; // Can't parse timestamp — treat as stale
    }
  }

  async loadDocuments(): Promise<void> {
    const recordId = this.id();
    if (!recordId) {
      return;
    }

    this.isLoadingDocuments.set(true);

    try {
      const response = await this.documentService.listDocuments(recordId);
      this.uploadedDocuments.set(response.documents);

      // Start polling for any documents that are still processing (and not stale)
      for (const doc of response.documents) {
        if (PROCESSING_STATUSES.includes(doc.status)) {
          // Skip polling for stale documents — the backend will auto-fail them
          // on the next fetch, so just let the current status show until refresh
          if (this.isDocumentStale(doc)) {
            continue;
          }
          // Only start polling if not already polling
          if (!this.pollingDocuments().has(doc.documentId)) {
            this.startPollingDocument(doc.documentId, recordId);
          }
        }
      }
    } catch (error) {
      console.error('Error loading documents:', error);
      // Don't show error to user, just log it
    } finally {
      this.isLoadingDocuments.set(false);
    }
  }

  async downloadDocument(documentId: string): Promise<void> {
    const recordId = this.id();
    if (!recordId) {
      return;
    }

    try {
      const response = await this.documentService.getDownloadUrl(recordId, documentId);
      window.open(response.downloadUrl, '_blank', 'noopener,noreferrer');
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to get download URL.';
      this.toast.error(message);
    }
  }

  async deleteDocument(documentId: string): Promise<void> {
    const recordId = this.id();
    if (!recordId) {
      return;
    }

    // Optimistic UI: drop the row immediately so the click feels instant
    // instead of waiting on the DELETE + full document-list reload. Soft-delete
    // is idempotent and almost always succeeds; on the rare failure we restore
    // the row and toast.
    const previousDocs = this.uploadedDocuments();
    if (!previousDocs.some((doc) => doc.documentId === documentId)) {
      return;
    }
    this.uploadedDocuments.update((docs) => docs.filter((doc) => doc.documentId !== documentId));
    // Drop from the polling set too so the row's spinner indicator doesn't
    // briefly reappear on the next poll tick before the GET 404s.
    this.pollingDocuments.update((set) => {
      const newSet = new Set(set);
      newSet.delete(documentId);
      return newSet;
    });

    try {
      await this.documentService.deleteDocument(recordId, documentId);
      // The backend cascades sync policies with their source — mirror that
      // locally so a covering policy's control disappears with the row.
      const covering = this.syncPolicyFor(documentId);
      if (covering) {
        this.removePolicy(covering.policyId);
      }
    } catch (error) {
      this.uploadedDocuments.set(previousDocs);
      const message = error instanceof Error ? error.message : 'Failed to delete document.';
      this.toast.error(message);
    }
  }

  /**
   * Remove a web source: the crawl record, every page it added to the
   * knowledge base, and any sync policy covering it. Confirmed first — unlike
   * a single document this can take dozens of pages with it, so the dialog
   * names the count.
   */
  async removeWebSource(crawl: CrawlJob): Promise<void> {
    const recordId = this.id();
    if (!recordId) {
      return;
    }

    const pages = crawl.fetchedCount;
    const pageCount = `${pages} page${pages === 1 ? '' : 's'}`;
    const dialogRef = this.dialog.open<boolean, ConfirmationDialogData>(
      ConfirmationDialogComponent,
      {
        data: {
          title: 'Remove web source',
          message:
            `${crawl.rootUrl} and the ${pageCount} it added will be removed from ` +
            `this knowledge base, along with any sync schedule on it. ` +
            `This cannot be undone.`,
          confirmText: 'Remove',
          destructive: true,
        },
      },
    );
    if ((await firstValueFrom(dialogRef.closed)) !== true) {
      return;
    }

    // Optimistic, like deleteDocument: drop the source and its pages up front
    // so the click lands immediately, and restore both lists if the call fails
    // (a still-running crawl is refused with a 409).
    const previousCrawls = this.webCrawls();
    const previousDocuments = this.uploadedDocuments();
    const pageDocuments = previousDocuments.filter((doc) => this.isPageOf(doc, crawl));

    this.webCrawls.update((crawls) => crawls.filter((c) => c.crawlId !== crawl.crawlId));
    this.uploadedDocuments.update((docs) => docs.filter((doc) => !this.isPageOf(doc, crawl)));
    // Stop polling any page still mid-processing, so its spinner can't
    // reappear on the next tick before the GET starts 404ing.
    this.pollingDocuments.update((set) => {
      const next = new Set(set);
      for (const doc of pageDocuments) {
        next.delete(doc.documentId);
      }
      return next;
    });

    try {
      await this.webSourceService.deleteCrawl(recordId, crawl.crawlId);
      // The backend cascades the sync policy with its source — mirror that
      // locally so the control disappears with the row.
      const covering = this.syncPolicyFor(crawl.crawlId);
      if (covering) {
        this.removePolicy(covering.policyId);
      }
      this.toast.success('Web source removed.');
    } catch (error) {
      this.webCrawls.set(previousCrawls);
      this.uploadedDocuments.set(previousDocuments);
      const message = error instanceof Error ? error.message : 'Failed to remove web source.';
      this.toast.error(message);
    }
  }

  /**
   * Whether a document is one of the pages a crawl produced. Mirrors the
   * backend's rule (a `web` document whose source URL sits under the crawl
   * root) so the optimistic removal matches what the server actually deletes.
   */
  private isPageOf(doc: Document, crawl: CrawlJob): boolean {
    return (
      doc.sourceConnectorId === 'web' && !!doc.sourceFileId?.startsWith(crawl.rootUrl)
    );
  }

  // ── KB sync policy actions ──────────────────────────────────────────────

  /** True while the reconnect consent popup is open (guards the abort effect). */
  private reconnectAwaiting = false;
  /** Source ref whose row is busy because of an in-flight reconnect. */
  private reconnectSourceRef: string | null = null;

  /**
   * Load sync policies + the crawl catalog for owners/editors. Best-effort: the
   * sync surface is secondary to the document editor, so a failure logs and
   * leaves the controls at their previous state instead of blocking.
   */
  private async loadSyncData(): Promise<void> {
    const recordId = this.id();
    if (!recordId || !this.canManageSync()) {
      // Viewers (and create mode) never load crawls — release the gate so the
      // document list can reveal.
      this.isLoadingCrawls.set(false);
      return;
    }
    try {
      const [policies, crawls] = await Promise.allSettled([
        this.syncPolicyService.listPolicies(recordId),
        this.webSourceService.listCrawls(recordId),
      ]);
      if (policies.status === 'fulfilled') {
        this.syncPolicies.set(policies.value);
      } else {
        console.error('Error loading sync policies:', policies.reason);
      }
      if (crawls.status === 'fulfilled') {
        this.webCrawls.set(crawls.value);
      } else {
        console.error('Error loading web sources:', crawls.reason);
      }
    } finally {
      this.isLoadingCrawls.set(false);
    }
  }

  syncPolicyFor(sourceRef: string): SyncPolicy | null {
    return this.policiesBySourceRef().get(sourceRef) ?? null;
  }

  isSyncBusy(sourceRef: string): boolean {
    return this.syncBusySourceRefs().has(sourceRef);
  }

  /**
   * A document is a syncable Drive-import source when it carries import
   * provenance from a real connector. Web pages carry the sentinel connector id
   * 'web' — those sync at the crawl level, not per page.
   */
  isDriveSyncable(doc: Document): boolean {
    return !!doc.sourceFileId && !!doc.sourceConnectorId && doc.sourceConnectorId !== 'web';
  }

  isCrawlSyncable(crawl: CrawlJob): boolean {
    return this.syncableCrawlStatuses.has(crawl.status);
  }

  /** Provider display name for a document's reconnect affordance. */
  reconnectLabelForDocument(doc: Document): string {
    const provider = this.fileSources().find((s) => s.providerId === doc.sourceConnectorId);
    return provider?.displayName ?? '';
  }

  async onSyncIntervalSelected(
    sourceType: SyncSourceType,
    sourceRef: string,
    selection: SyncIntervalSelection,
  ): Promise<void> {
    const recordId = this.id();
    if (!recordId) {
      return;
    }
    const existing = this.syncPolicyFor(sourceRef);
    this.setSyncBusy(sourceRef, true);
    try {
      if (selection === 'manual') {
        if (existing) {
          await this.syncPolicyService.deletePolicy(recordId, existing.policyId);
          this.removePolicy(existing.policyId);
        }
      } else if (existing) {
        this.upsertPolicy(
          await this.syncPolicyService.updatePolicy(recordId, existing.policyId, {
            interval: selection,
          }),
        );
      } else {
        this.upsertPolicy(
          await this.syncPolicyService.createPolicy(recordId, {
            sourceType,
            sourceRef,
            interval: selection,
          }),
        );
      }
    } catch (error) {
      this.toastSyncError(error, 'Could not update sync settings.');
      // A duplicate/not-found conflict means our local view is stale — converge.
      void this.loadSyncData();
    } finally {
      this.setSyncBusy(sourceRef, false);
    }
  }

  async onSyncPause(sourceRef: string): Promise<void> {
    await this.patchSyncState(sourceRef, 'paused_user');
  }

  async onSyncResume(sourceRef: string): Promise<void> {
    await this.patchSyncState(sourceRef, 'active');
  }

  private async patchSyncState(
    sourceRef: string,
    state: 'active' | 'paused_user',
  ): Promise<void> {
    const recordId = this.id();
    const existing = this.syncPolicyFor(sourceRef);
    if (!recordId || !existing) {
      return;
    }
    this.setSyncBusy(sourceRef, true);
    try {
      this.upsertPolicy(
        await this.syncPolicyService.updatePolicy(recordId, existing.policyId, { state }),
      );
    } catch (error) {
      this.toastSyncError(
        error,
        state === 'active' ? 'Could not resume sync.' : 'Could not pause sync.',
      );
      void this.loadSyncData();
    } finally {
      this.setSyncBusy(sourceRef, false);
    }
  }

  async onSyncRunNow(sourceRef: string): Promise<void> {
    const recordId = this.id();
    const existing = this.syncPolicyFor(sourceRef);
    if (!recordId || !existing) {
      return;
    }
    this.setSyncBusy(sourceRef, true);
    try {
      this.upsertPolicy(await this.syncPolicyService.runNow(recordId, existing.policyId));
      this.toast.success('Sync requested — it will run within about 15 minutes.');
    } catch (error) {
      // 429 (cooldown) and 409 (not active) both carry a user-appropriate
      // detail message from the server — surface it as-is.
      this.toastSyncError(error, 'Could not request a sync.');
    } finally {
      this.setSyncBusy(sourceRef, false);
    }
  }

  /**
   * "Reconnect <provider>" for a paused_reauth policy. Runs the same OAuth
   * consent popup as the connector buttons; the backend's consent-complete hook
   * flips the paused policies back to active, so on success we only refetch.
   * Reconnect is a Drive-source affair — web crawls fetch anonymously and can
   * never enter paused_reauth.
   */
  async onSyncReconnect(sourceRef: string): Promise<void> {
    const doc = this.uploadedDocuments().find((d) => d.documentId === sourceRef);
    const providerId = doc?.sourceConnectorId;
    if (!providerId || providerId === 'web') {
      this.toast.error('This source cannot be reconnected from here.');
      return;
    }
    this.reconnectingProviderId.set(providerId);
    this.reconnectSourceRef = sourceRef;
    this.setSyncBusy(sourceRef, true);
    try {
      const result = await this.connectorsService.initiateConsent(providerId);
      if (result.connected) {
        // The vault thinks the token is fine (provider-side revocations are
        // invisible to it) — no consent popup will open, so the resume hook
        // won't fire. Refetch and tell the user what actually happened.
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
    } catch (error) {
      this.finishReconnect();
      const message =
        error instanceof Error ? error.message : 'Could not start the reconnect flow.';
      this.toast.error(message);
    }
  }

  /** Reset all reconnect bookkeeping (success, failure, or aborted popup). */
  private finishReconnect(): void {
    this.reconnectingProviderId.set(null);
    this.reconnectAwaiting = false;
    if (this.reconnectSourceRef) {
      this.setSyncBusy(this.reconnectSourceRef, false);
      this.reconnectSourceRef = null;
    }
  }

  private setSyncBusy(sourceRef: string, busy: boolean): void {
    this.syncBusySourceRefs.update((set) => {
      const next = new Set(set);
      if (busy) {
        next.add(sourceRef);
      } else {
        next.delete(sourceRef);
      }
      return next;
    });
  }

  private upsertPolicy(policy: SyncPolicy): void {
    this.syncPolicies.update((list) => {
      const exists = list.some((p) => p.policyId === policy.policyId);
      return exists
        ? list.map((p) => (p.policyId === policy.policyId ? policy : p))
        : [...list, policy];
    });
  }

  private removePolicy(policyId: string): void {
    this.syncPolicies.update((list) => list.filter((p) => p.policyId !== policyId));
  }

  private toastSyncError(error: unknown, fallback: string): void {
    const message = error instanceof Error && error.message ? error.message : fallback;
    this.toast.error(message);
  }

  async startPollingDocument(documentId: string, recordId: string): Promise<void> {
    // Add to polling set
    this.pollingDocuments.update((set) => new Set(set).add(documentId));

    try {
      await this.documentService.pollDocumentStatus(recordId, documentId, (document) => {
        // Update the document in the list
        this.uploadedDocuments.update((docs) =>
          docs.map((doc) => (doc.documentId === documentId ? document : doc)),
        );
      });

      // Polling completed - reload full list to ensure consistency
      await this.loadDocuments();
    } catch (error) {
      // Handle document/record deletion gracefully
      if (error instanceof DocumentUploadError && error.code === 'DOCUMENT_NOT_FOUND') {
        console.warn('Document or record was deleted during polling:', documentId);
        // Remove the document from the local list immediately
        this.uploadedDocuments.update((docs) =>
          docs.filter((doc) => doc.documentId !== documentId),
        );
      } else {
        console.error('Error polling document status:', error);
        // Reload list anyway to get current state
        await this.loadDocuments();
      }
    } finally {
      // Remove from polling set
      this.pollingDocuments.update((set) => {
        const newSet = new Set(set);
        newSet.delete(documentId);
        return newSet;
      });
    }
  }

  formatBytes(bytes: number): string {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
  }
}
