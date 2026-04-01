import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
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
import { Subscription } from 'rxjs';
import { AssistantService } from '../services/assistant.service';
import { DocumentService, DocumentUploadError } from '../services/document.service';
import { Document, PROCESSING_STATUSES, STALE_DOCUMENT_THRESHOLD_MS } from '../models/document.model';
import { AssistantPreviewComponent } from './components/assistant-preview.component';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroChevronRight,
  heroFaceSmile,
  heroXMark,
  heroUserGroup,
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
  ],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroChevronRight,
      heroFaceSmile,
      heroXMark,
      heroUserGroup,
    }),
  ],
})
export class AssistantFormPage implements OnInit, OnDestroy {
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  private fb = inject(FormBuilder);
  private assistantService = inject(AssistantService);
  private documentService = inject(DocumentService);
  readonly sidenavService = inject(SidenavService);
  private readonly themeService = inject(ThemeService);
  private readonly dialog = inject(Dialog);

  // Emoji picker popover state
  readonly isEmojiPickerOpen = signal(false);

  // Expose theme for emoji picker dark mode
  readonly isDarkMode = this.themeService.theme;

  readonly assistantId = signal<string | null>(null);
  readonly mode = computed<'create' | 'edit'>(() => (this.assistantId() ? 'edit' : 'create'));

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

    // If editing, load the assistant data and documents
    if (id) {
      this.loadAssistant(id);
      this.loadDocuments();
    }

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
        // Create a draft assistant first
        const draft = await this.assistantService.createDraft({
          name: this.form.get('name')?.value || 'Untitled Assistant',
        });
        assistantId = draft.assistantId;
        this.assistantId.set(assistantId);

        // Update form with draft data
        this.form.patchValue({
          name: draft.name,
          description: draft.description || '',
          instructions: draft.instructions || '',
          vectorIndexId: draft.vectorIndexId,
          visibility: draft.visibility,
          tags: draft.tags,
          status: draft.status,
        });
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

  async deleteDocument(documentId: string): Promise<void> {
    const assistantId = this.assistantId();
    if (!assistantId) {
      return;
    }

    try {
      await this.documentService.deleteDocument(assistantId, documentId);
      // Reload documents list
      await this.loadDocuments();
    } catch (error) {
      console.error('Error deleting document:', error);
      // TODO: Show error message to user
    }
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
    const baseClasses = 'inline-flex items-center rounded-xs px-2.5 py-1 text-xs/5 font-medium';

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
