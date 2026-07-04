import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
  effect,
  OnInit,
  OnDestroy,
} from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import {
  ReactiveFormsModule,
  FormBuilder,
  FormGroup,
  FormArray,
  FormControl,
  Validators,
} from '@angular/forms';
import { Subscription, firstValueFrom } from 'rxjs';
import { AssistantService } from '../services/assistant.service';
import { DocumentService, DocumentUploadError } from '../services/document.service';
import { Document, PROCESSING_STATUSES, STALE_DOCUMENT_THRESHOLD_MS } from '../models/document.model';
import { AssistantPreviewComponent } from './components/assistant-preview.component';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowDownTray,
  heroArrowLeft,
  heroArrowPath,
  heroChevronRight,
  heroFaceSmile,
  heroGlobeAlt,
  heroLink,
  heroXMark,
  heroUser,
  heroUserGroup,
  heroPlus,
  heroTrash,
} from '@ng-icons/heroicons/outline';
import { Dialog } from '@angular/cdk/dialog';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { PickerComponent } from '@ctrl/ngx-emoji-mart';
import { CdkConnectedOverlay, CdkOverlayOrigin, ConnectedPosition } from '@angular/cdk/overlay';
import { ThemeService } from '../../components/topnav/components/theme-toggle/theme.service';
import {
  ShareAssistantDialogComponent,
  ShareAssistantDialogData,
} from '../components/share-assistant-dialog.component';
import {
  FileSourceBrowserDialogComponent,
  FileSourceBrowserDialogData,
} from '../components/file-source-browser-dialog.component';
import {
  WebSourceDialogComponent,
  WebSourceDialogData,
} from '../components/web-source-dialog.component';
import { FileSourceService } from '../services/file-source.service';
import { WebSourceService } from '../services/web-source.service';
import { SyncPolicyService } from '../services/sync-policy.service';
import { FileSourceConnector } from '../models/file-source.model';
import { CrawlJob } from '../models/web-source.model';
import { SyncPolicy, SyncSourceType } from '../models/sync-policy.model';
import {
  SyncPolicyControlComponent,
  SyncIntervalSelection,
} from '../components/sync-policy-control.component';
import { UserConnectorsService } from '../../settings/connectors/services/user-connectors.service';
import { OAuthConsentService } from '../../services/oauth-consent/oauth-consent.service';
import { ToastService } from '../../services/toast/toast.service';

@Component({
  selector: 'app-assistant-form-page',
  templateUrl: './assistant-form.page.html',
  styleUrl: './assistant-form.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ReactiveFormsModule,
    AssistantPreviewComponent,
    NgIcon,
    RouterLink,
    PickerComponent,
    CdkOverlayOrigin,
    CdkConnectedOverlay,
    SyncPolicyControlComponent,
  ],
  providers: [
    provideIcons({
      heroArrowDownTray,
      heroArrowLeft,
      heroArrowPath,
      heroChevronRight,
      heroFaceSmile,
      heroGlobeAlt,
      heroLink,
      heroXMark,
      heroUser,
      heroUserGroup,
      heroPlus,
      heroTrash,
    }),
  ],
})
export class AssistantFormPage implements OnInit, OnDestroy {
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  private fb = inject(FormBuilder);
  private assistantService = inject(AssistantService);
  private documentService = inject(DocumentService);
  private fileSourceService = inject(FileSourceService);
  private webSourceService = inject(WebSourceService);
  private syncPolicyService = inject(SyncPolicyService);
  private readonly connectorsService = inject(UserConnectorsService);
  private readonly consentService = inject(OAuthConsentService);
  readonly sidenavService = inject(SidenavService);
  private readonly themeService = inject(ThemeService);
  private readonly dialog = inject(Dialog);
  private readonly toast = inject(ToastService);

  // Emoji picker popover state
  readonly isEmojiPickerOpen = signal(false);

  // Expose theme for emoji picker dark mode
  readonly isDarkMode = this.themeService.theme;

  readonly assistantId = signal<string | null>(null);
  readonly mode = computed<'create' | 'edit'>(() => (this.assistantId() ? 'edit' : 'create'));
  /** The requesting user's permission on the loaded assistant — populated by loadAssistant.
   *  In create mode the user is implicitly the owner, so we seed it that way. */
  readonly userPermission = signal<'owner' | 'editor' | 'viewer'>('owner');
  /** Owner display name surfaced on the editor banner when the requester is an editor. */
  readonly ownerName = signal<string>('');
  readonly canManageShares = computed(() => this.userPermission() === 'owner');
  readonly isEditorView = computed(() => this.userPermission() === 'editor');

  // Live form value signals — kept in sync via form.valueChanges so the
  // preview component (OnPush) receives updates as the user types.
  readonly liveFormName = signal('');
  readonly liveFormDescription = signal('');
  readonly liveFormInstructions = signal('');
  readonly liveFormEmoji = signal('');
  readonly liveFormStarters = signal<string[]>([]);

  private formSub?: Subscription;

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
   * True while the editor is fetching the connector catalog for the first
   * time. Drives the inline skeleton chips so the connector buttons fade
   * in rather than popping into existence after a network round-trip.
   * Initial value `true` because `loadFileSources()` is called from
   * `ngOnInit` — the row should render the skeleton on first paint.
   */
  readonly fileSourcesLoading = signal<boolean>(true);

  /** Provider whose consent popup is in flight from an editor connector button. */
  readonly connectingProviderId = signal<string | null>(null);
  readonly connectPhase = signal<'initiating' | 'awaiting' | null>(null);

  /**
   * True while a web crawl is running for this assistant. Drives a small
   * "crawling…" badge in the Knowledge section so the user knows pages will
   * keep appearing for a while after the dialog closes.
   */
  readonly webCrawlActive = signal<boolean>(false);
  private crawlWatcherHandle: ReturnType<typeof setInterval> | null = null;

  // ── KB sync policies ────────────────────────────────────────────────────

  /** Sync policies covering this assistant's sources. Loaded for owners/editors only. */
  readonly syncPolicies = signal<SyncPolicy[]>([]);
  /** All crawl jobs (any status) — completed crawls are the syncable web sources. */
  readonly webCrawls = signal<CrawlJob[]>([]);
  /** Source refs (document/crawl ids) with a sync mutation in flight. */
  readonly syncBusySourceRefs = signal<Set<string>>(new Set());
  /** Provider whose consent popup was opened from a "Reconnect" affordance. */
  readonly reconnectingProviderId = signal<string | null>(null);

  /**
   * Sync controls are owner/editor-only: the backend edit-gates the whole
   * sync-policy surface, so viewers never see the controls (and we never
   * issue the list call that would 403 for them).
   */
  readonly canManageSync = computed(
    () => this.mode() === 'edit' && this.userPermission() !== 'viewer',
  );

  private readonly policiesBySourceRef = computed(
    () => new Map(this.syncPolicies().map((p) => [p.sourceRef, p])),
  );

  /**
   * Crawls that can carry a sync policy. A `running` crawl is excluded —
   * its page set is still forming — but it still renders in the web-sources
   * list with a progress note.
   */
  readonly syncableCrawlStatuses: ReadonlySet<CrawlJob['status']> = new Set([
    'complete',
    'failed',
  ]);

  /**
   * True once at least one document exists, is uploading, or is still
   * loading. Drives swapping the full drop zone for a compact "Add files"
   * control — the drop zone only shows while there is nothing to display.
   */
  readonly hasDocuments = computed(
    () =>
      this.uploadedDocuments().length > 0 ||
      this.currentUpload() !== null ||
      this.isLoadingDocuments(),
  );

  form!: FormGroup;

  // Emoji picker positioning - opens below and to the right
  readonly emojiPickerPositions: ConnectedPosition[] = [
    {
      originX: 'start',
      originY: 'bottom',
      overlayX: 'start',
      overlayY: 'top',
      offsetY: 8,
    },
    {
      originX: 'start',
      originY: 'top',
      overlayX: 'start',
      overlayY: 'bottom',
      offsetY: -8,
    },
  ];

  get starters(): FormArray {
    return this.form.get('starters') as FormArray;
  }

  constructor() {
    // Resolve the OAuth consent popup for a connector kicked off from an
    // editor button. Mirrors the file-source browser dialog's effect so the
    // editor can drive the flow without opening the modal first.
    effect(() => {
      const completion = this.consentService.completion();
      if (!completion || !completion.providerId) {
        return;
      }
      // A consent kicked off from a sync-control "Reconnect" affordance:
      // a fresh consent auto-resumes paused_reauth policies server-side,
      // so all that's left here is to refetch and confirm.
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

    // If the user closes the popup without finishing, the consent service
    // drops the provider from `inFlightProviders` — reset the button state.
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
  }

  ngOnInit(): void {
    // Hide sidenav when entering the form page
    this.sidenavService.hide();

    // Check if we're editing an existing assistant
    const id = this.route.snapshot.paramMap.get('id');
    this.assistantId.set(id);

    // Initialize the form with all required fields
    this.form = this.fb.group({
      name: ['', [Validators.required, Validators.minLength(3)]],
      description: ['', [Validators.required, Validators.minLength(10)]],
      instructions: ['', [Validators.required, Validators.minLength(20)]],
      vectorIndexId: ['idx_assistants', [Validators.required]],
      visibility: ['PRIVATE'],
      tags: [[]],
      starters: this.fb.array([]),
      emoji: [''],
      status: ['DRAFT'],
    });

    // If editing, load the assistant data and documents. Sync data waits on
    // loadAssistant because it needs the resolved userPermission — the
    // sync-policy surface is edit-gated and viewers must not trigger a 403.
    if (id) {
      void this.loadAssistant(id).then(() => this.loadSyncData());
      this.loadDocuments();
    }

    // Load the connectors the user can import documents from (create or edit)
    void this.loadFileSources();

    // Sync form changes into signals so the preview (OnPush) updates live
    this.syncFormToSignals();
    this.formSub = this.form.valueChanges.subscribe(() => this.syncFormToSignals());
  }

  /** Push current form values into the live signals */
  private syncFormToSignals(): void {
    this.liveFormName.set(this.form.get('name')?.value || '');
    this.liveFormDescription.set(this.form.get('description')?.value || '');
    this.liveFormInstructions.set(this.form.get('instructions')?.value || '');
    this.liveFormEmoji.set(this.form.get('emoji')?.value || '');
    this.liveFormStarters.set(this.starters.value || []);
  }

  ngOnDestroy(): void {
    // Show sidenav when leaving the form page
    this.sidenavService.show();
    this.formSub?.unsubscribe();
    this.stopCrawlWatcher();
  }

  async loadAssistant(id: string): Promise<void> {
    try {
      // First check local cache
      let assistant = this.assistantService.getAssistantById(id);

      // If not in cache, fetch from API
      if (!assistant) {
        const response = await this.assistantService.getAssistant(id);
        assistant = response;
      }

      if (assistant) {
        this.form.patchValue({
          name: assistant.name,
          description: assistant.description,
          instructions: assistant.instructions,
          vectorIndexId: assistant.vectorIndexId,
          visibility: assistant.visibility,
          tags: assistant.tags,
          emoji: assistant.emoji || '',
          status: assistant.status,
        });

        // Cached assistants from the list view do not carry userPermission
        // (the list synthesises it locally) — fall back to 'owner' for cache hits
        // so the owner's editor experience stays identical.
        this.userPermission.set(assistant.userPermission ?? 'owner');
        this.ownerName.set(assistant.ownerName ?? '');

        // Populate starters FormArray
        this.starters.clear();
        if (assistant.starters && assistant.starters.length > 0) {
          assistant.starters.forEach((starter) => {
            this.starters.push(new FormControl(starter, Validators.required));
          });
        }
      }
    } catch (error) {
      console.error('Error loading assistant:', error);
      // TODO: Show error message to user
    }
  }

  async onSubmit(): Promise<void> {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }

    const formData = this.form.value;

    try {
      if (this.mode() === 'create') {
        // For create mode, we don't have an ID yet
        // Use createAssistant which will generate one
        await this.assistantService.createAssistant(formData);
      } else {
        // For edit mode, update the existing assistant
        // Set status to COMPLETE when saving from draft
        const updateData = {
          ...formData,
          status: 'COMPLETE' as const,
        };
        await this.assistantService.updateAssistant(this.assistantId()!, updateData);
      }

      // Navigate back to assistants list
      this.router.navigate(['/assistants']);
    } catch (error) {
      console.error('Error saving assistant:', error);
      // TODO: Show error message to user
    }
  }

  onCancel(): void {
    this.router.navigate(['/assistants']);
  }

  addStarter(): void {
    this.starters.push(new FormControl('', Validators.required));
  }

  removeStarter(index: number): void {
    this.starters.removeAt(index);
  }

  getFieldError(fieldName: string): string | null {
    const field = this.form.get(fieldName);
    if (!field || !field.touched || !field.errors) {
      return null;
    }

    if (field.errors['required']) {
      return 'This field is required';
    }
    if (field.errors['minlength']) {
      const minLength = field.errors['minlength'].requiredLength;
      return `Minimum length is ${minLength} characters`;
    }

    return null;
  }

  toggleEmojiPicker(): void {
    this.isEmojiPickerOpen.update((open) => !open);
  }

  closeEmojiPicker(): void {
    this.isEmojiPickerOpen.set(false);
  }

  onEmojiSelect(event: { emoji: { native: string } }): void {
    this.form.patchValue({ emoji: event.emoji.native });
    this.closeEmojiPicker();
  }

  clearEmoji(): void {
    this.form.patchValue({ emoji: '' });
  }

  openShareDialog(): void {
    const assistantId = this.assistantId();
    if (!assistantId) return;

    // Build a minimal assistant object from the current form state
    const assistant = {
      assistantId,
      name: this.form.get('name')?.value || '',
      visibility: this.form.get('visibility')?.value || 'PRIVATE',
    } as import('../models/assistant.model').Assistant;

    this.dialog.open<unknown, ShareAssistantDialogData>(ShareAssistantDialogComponent, {
      data: { assistant },
      hasBackdrop: false,
    });
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
      // Clear the input
      input.value = '';
      return;
    }

    // Ensure we have an assistant ID (create draft if in create mode)
    let assistantId = this.assistantId();
    if (!assistantId) {
      try {
        assistantId = await this.createDraftAssistant();
      } catch (error) {
        const errorMessage = error instanceof Error ? error.message : 'Failed to create assistant';
        this.currentUpload.set({
          file,
          progress: 0,
          status: 'error',
          error: errorMessage,
        });
        input.value = '';
        return;
      }
    }

    // Upload the document
    await this.uploadDocument(file, assistantId);

    // Clear the input to allow re-selecting the same file
    input.value = '';
  }

  /**
   * Ensure an assistant record exists so documents have a parent to attach
   * to. In create mode the form has no assistant yet, so a draft is created
   * and the form is patched with its server-assigned fields. Returns the
   * assistant id; throws if draft creation fails.
   */
  private async createDraftAssistant(): Promise<string> {
    const draft = await this.assistantService.createDraft({
      name: this.form.get('name')?.value || 'Untitled Assistant',
    });
    this.assistantId.set(draft.assistantId);
    this.form.patchValue({
      name: draft.name,
      description: draft.description || '',
      instructions: draft.instructions || '',
      vectorIndexId: draft.vectorIndexId,
      visibility: draft.visibility,
      tags: draft.tags,
      status: draft.status,
    });
    return draft.assistantId;
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
   * Click handler for an editor connector button: browse when the source is
   * already connected; otherwise kick off the OAuth consent flow in place so
   * the user doesn't have to open the modal just to connect.
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
   * success the browser modal opens automatically — see the completion
   * effect → `afterConnect`. Mirrors the dialog's `connect()` path.
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
   * After a successful consent, refresh the file-source list (so the
   * connector now reports `connected: true`) and open the browser modal
   * straight into it so the user can pick files without a second click.
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
   * opens straight into it, skipping the in-modal source picker. Ensures a
   * draft assistant exists first — imported documents need a parent. On
   * close, any imported documents are merged into the list and polled like a
   * device upload.
   */
  async openFileSourceBrowser(connector?: FileSourceConnector): Promise<void> {
    let assistantId = this.assistantId();
    if (!assistantId) {
      try {
        assistantId = await this.createDraftAssistant();
      } catch (error) {
        const message = error instanceof Error ? error.message : 'Failed to create assistant';
        this.toast.error(message);
        return;
      }
    }

    const dialogRef = this.dialog.open<Document[] | undefined, FileSourceBrowserDialogData>(
      FileSourceBrowserDialogComponent,
      {
        data: { assistantId, connector },
        hasBackdrop: false,
      },
    );

    const imported = await firstValueFrom(dialogRef.closed);
    if (imported && imported.length > 0) {
      this.toast.success(
        `Importing ${imported.length} file${imported.length === 1 ? '' : 's'}…`,
      );
      // loadDocuments() picks up the new 'uploading' records and starts
      // polling them through to 'complete', exactly like a device upload.
      await this.loadDocuments();
    }
  }

  /**
   * Open the web-source dialog so the user can attach a URL (single page or
   * a bounded crawl). Mirrors {@link openFileSourceBrowser} — ensures a
   * draft assistant exists, opens the dialog, then on close merges the
   * pre-created root document into the list and starts the crawl watcher
   * so additional pages surface as the crawler discovers them.
   */
  async openWebSourceDialog(): Promise<void> {
    let assistantId = this.assistantId();
    if (!assistantId) {
      try {
        assistantId = await this.createDraftAssistant();
      } catch (error) {
        const message =
          error instanceof Error ? error.message : 'Failed to create assistant';
        this.toast.error(message);
        return;
      }
    }

    const dialogRef = this.dialog.open<Document[] | undefined, WebSourceDialogData>(
      WebSourceDialogComponent,
      {
        data: { assistantId },
        hasBackdrop: false,
      },
    );

    const imported = await firstValueFrom(dialogRef.closed);
    if (imported && imported.length > 0) {
      this.toast.success('Crawling web content…');
      await this.loadDocuments();
      this.startCrawlWatcher();
      // Surface the new crawl in the web-sources list right away (it shows
      // as running until the watcher sees it finish).
      void this.loadSyncData();
    }
  }

  /**
   * Poll active crawls for this assistant every few seconds. While any are
   * `running` we surface newly-discovered pages via {@link discoverNewDocuments}
   * — an *incremental* merge that appends only the new rows. The full list
   * is not replaced, so unchanged rows keep their references and per-doc
   * polling drives status updates without a list-wide flicker. Stops itself
   * once the server reports no active crawls.
   */
  private startCrawlWatcher(): void {
    this.webCrawlActive.set(true);
    this.stopCrawlWatcher();
    const tick = async (): Promise<void> => {
      const assistantId = this.assistantId();
      if (!assistantId) {
        this.stopCrawlWatcher();
        return;
      }
      try {
        const active = await this.webSourceService.listActiveCrawls(assistantId);
        if (active.length === 0) {
          this.webCrawlActive.set(false);
          this.stopCrawlWatcher();
          // Catch any pages that completed in the gap between the previous
          // tick and the server reporting "no crawls running" — still
          // incremental, so no list-wide refresh.
          await this.discoverNewDocuments();
          // The crawl just went terminal — refresh the web-sources list so
          // its row flips from "Crawling…" to a syncable source.
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
   * Fetch the assistant's documents and append only the IDs we don't already
   * have to the local list — does NOT replace existing rows. Each new
   * processing doc gets its own per-doc polling started so its status
   * updates flow into the list one row at a time, no list-wide refresh.
   *
   * Used by the crawl watcher: a crawl discovers pages over its lifetime,
   * and we want each new page to slide into the list when it appears
   * without re-rendering rows that already exist.
   */
  private async discoverNewDocuments(): Promise<void> {
    const assistantId = this.assistantId();
    if (!assistantId) {
      return;
    }
    try {
      const response = await this.documentService.listDocuments(assistantId);
      const existing = new Set(
        this.uploadedDocuments().map((doc) => doc.documentId),
      );
      const newDocs = response.documents.filter(
        (doc) => !existing.has(doc.documentId),
      );
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
          this.startPollingDocument(doc.documentId, assistantId);
        }
      }
    } catch (error) {
      console.error('Error discovering new documents:', error);
    }
  }

  async uploadDocument(file: File, assistantId: string): Promise<void> {
    // Set initial upload state
    this.currentUpload.set({
      file,
      progress: 0,
      status: 'uploading',
    });

    let documentId: string | undefined;

    try {
      // Step 1: Request presigned URL
      const uploadUrlResponse = await this.documentService.requestUploadUrl(assistantId, file);
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
      this.startPollingDocument(uploadUrlResponse.documentId, assistantId);

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

      // Report the failure to the backend so the DynamoDB record is marked
      // as 'failed' instead of stuck in 'uploading'. This prevents infinite
      // polling on page refresh.
      if (documentId) {
        const details =
          error instanceof DocumentUploadError
            ? JSON.stringify(error.details)
            : undefined;
        this.documentService.reportUploadFailure(assistantId, documentId, errorMessage, details);
      }
    }
  }

  /**
   * Check if a document in a processing state is stale (updatedAt too old).
   * Matches the backend's 10-minute threshold so the frontend can skip
   * polling for documents that the backend will auto-fail on next fetch.
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
    const assistantId = this.assistantId();
    if (!assistantId) {
      return;
    }

    this.isLoadingDocuments.set(true);

    try {
      const response = await this.documentService.listDocuments(assistantId);
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
            this.startPollingDocument(doc.documentId, assistantId);
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
    const assistantId = this.assistantId();
    if (!assistantId) {
      return;
    }

    try {
      const response = await this.documentService.getDownloadUrl(assistantId, documentId);
      window.open(response.downloadUrl, '_blank', 'noopener,noreferrer');
    } catch (error) {
      const message =
        error instanceof Error ? error.message : 'Failed to get download URL.';
      this.toast.error(message);
    }
  }

  async deleteDocument(documentId: string): Promise<void> {
    const assistantId = this.assistantId();
    if (!assistantId) {
      return;
    }

    // Optimistic UI: drop the row immediately so the click feels instant
    // instead of waiting on the DELETE + full document-list reload (a couple
    // seconds round-trip). Soft-delete is idempotent and almost always
    // succeeds for a doc the user owns and is currently looking at; on the
    // rare failure we restore the row and toast.
    const previousDocs = this.uploadedDocuments();
    if (!previousDocs.some((doc) => doc.documentId === documentId)) {
      return;
    }
    this.uploadedDocuments.update((docs) =>
      docs.filter((doc) => doc.documentId !== documentId),
    );
    // Drop from the polling set too so the row's spinner indicator doesn't
    // briefly reappear on the next poll tick before the GET 404s.
    this.pollingDocuments.update((set) => {
      const newSet = new Set(set);
      newSet.delete(documentId);
      return newSet;
    });

    try {
      await this.documentService.deleteDocument(assistantId, documentId);
      // The backend cascades sync policies with their source — mirror that
      // locally so a covering policy's control disappears with the row.
      const covering = this.syncPolicyFor(documentId);
      if (covering) {
        this.removePolicy(covering.policyId);
      }
    } catch (error) {
      this.uploadedDocuments.set(previousDocs);
      const message =
        error instanceof Error ? error.message : 'Failed to delete document.';
      this.toast.error(message);
    }
  }

  // ── KB sync policy actions ──────────────────────────────────────────────

  /** True while the reconnect consent popup is open (guards the abort effect). */
  private reconnectAwaiting = false;
  /** Source ref whose row is busy because of an in-flight reconnect. */
  private reconnectSourceRef: string | null = null;

  /**
   * Load sync policies + the crawl catalog for owners/editors. Best-effort:
   * the sync surface is secondary to the document editor, so a failure logs
   * and leaves the controls at their previous state instead of blocking.
   */
  private async loadSyncData(): Promise<void> {
    const assistantId = this.assistantId();
    if (!assistantId || !this.canManageSync()) {
      return;
    }
    const [policies, crawls] = await Promise.allSettled([
      this.syncPolicyService.listPolicies(assistantId),
      this.webSourceService.listCrawls(assistantId),
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
  }

  syncPolicyFor(sourceRef: string): SyncPolicy | null {
    return this.policiesBySourceRef().get(sourceRef) ?? null;
  }

  isSyncBusy(sourceRef: string): boolean {
    return this.syncBusySourceRefs().has(sourceRef);
  }

  /**
   * A document is a syncable Drive-import source when it carries import
   * provenance from a real connector. Web pages carry the sentinel
   * connector id 'web' — those sync at the crawl level, not per page.
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
    const assistantId = this.assistantId();
    if (!assistantId) {
      return;
    }
    const existing = this.syncPolicyFor(sourceRef);
    this.setSyncBusy(sourceRef, true);
    try {
      if (selection === 'manual') {
        if (existing) {
          await this.syncPolicyService.deletePolicy(assistantId, existing.policyId);
          this.removePolicy(existing.policyId);
        }
      } else if (existing) {
        this.upsertPolicy(
          await this.syncPolicyService.updatePolicy(assistantId, existing.policyId, {
            interval: selection,
          }),
        );
      } else {
        this.upsertPolicy(
          await this.syncPolicyService.createPolicy(assistantId, {
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
    const assistantId = this.assistantId();
    const existing = this.syncPolicyFor(sourceRef);
    if (!assistantId || !existing) {
      return;
    }
    this.setSyncBusy(sourceRef, true);
    try {
      this.upsertPolicy(
        await this.syncPolicyService.updatePolicy(assistantId, existing.policyId, { state }),
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
    const assistantId = this.assistantId();
    const existing = this.syncPolicyFor(sourceRef);
    if (!assistantId || !existing) {
      return;
    }
    this.setSyncBusy(sourceRef, true);
    try {
      this.upsertPolicy(await this.syncPolicyService.runNow(assistantId, existing.policyId));
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
   * consent popup as the connector buttons; the backend's consent-complete
   * hook flips the paused policies back to active, so on success we only
   * refetch. Reconnect is a Drive-source affair — web crawls fetch
   * anonymously and can never enter paused_reauth.
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

  async startPollingDocument(documentId: string, assistantId: string): Promise<void> {
    // Add to polling set
    this.pollingDocuments.update((set) => new Set(set).add(documentId));

    try {
      await this.documentService.pollDocumentStatus(assistantId, documentId, (document) => {
        // Update the document in the list
        this.uploadedDocuments.update((docs) =>
          docs.map((doc) => (doc.documentId === documentId ? document : doc)),
        );
      });

      // Polling completed - reload full list to ensure consistency
      await this.loadDocuments();
    } catch (error) {
      // Handle document/assistant deletion gracefully
      if (error instanceof DocumentUploadError && error.code === 'DOCUMENT_NOT_FOUND') {
        console.warn('Document or assistant was deleted during polling:', documentId);
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

  getStatusBadgeClasses(): string {
    const status = this.form?.get('status')?.value || 'DRAFT';
    const baseClasses = 'inline-flex items-center rounded-2xl px-2.5 py-0.5 text-xs/5 font-medium';

    switch (status) {
      case 'COMPLETE':
        return `${baseClasses} bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300`;
      case 'DRAFT':
        return `${baseClasses} bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300`;
      default:
        return `${baseClasses} bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300`;
    }
  }
}
