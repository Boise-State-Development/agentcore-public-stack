import { Component, signal, output, inject, input, computed } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroPlus,
  heroAdjustmentsHorizontal,
  heroClock,
  heroStop
} from '@ng-icons/heroicons/outline';
import { heroPaperAirplaneSolid } from '@ng-icons/heroicons/solid';
import { ChatStateService } from '../../services/chat/chat-state.service';
import { ModelDropdownComponent } from '../../../components/model-dropdown/model-dropdown.component';
import { QuotaWarningBannerComponent } from '../../../components/quota-warning-banner/quota-warning-banner.component';
import { TooltipDirective } from '../../../components/tooltip';
import { FileCardComponent } from '../../../components/file-card';
import { StorageQuotaBannerComponent } from '../../../components/storage-quota-banner';
import {
  FileUploadService,
  PendingUpload,
  ALLOWED_EXTENSIONS,
  MAX_FILE_SIZE_BYTES,
  MAX_FILES_PER_MESSAGE,
  formatBytes
} from '../../../services/file-upload';
import { ToastService } from '../../../services/toast/toast.service';

interface Message {
  content: string;
  timestamp: Date;
  fileUploadIds?: string[];
}

@Component({
  selector: 'app-chat-input',
  imports: [FormsModule, ModelDropdownComponent, NgIcon, QuotaWarningBannerComponent, StorageQuotaBannerComponent, TooltipDirective, FileCardComponent],
  providers: [
    provideIcons({
      heroPlus,
      heroAdjustmentsHorizontal,
      heroClock,
      heroStop,
      heroPaperAirplaneSolid
    })
  ],
  templateUrl: './chat-input.component.html',
  styleUrl: './chat-input.component.css'
})
export class ChatInputComponent {
  // Service injection
  readonly chatState = inject(ChatStateService);
  private readonly fileUploadService = inject(FileUploadService);
  private readonly toastService = inject(ToastService);

  // Input: session ID for file uploads
  readonly sessionId = input<string | null>(null);

  // Signals for state management
  userInput = signal('');
  isExpanded = signal(false);
  isFocused = signal(false);
  isDraggingOver = signal(false);

  // Track drag enter/leave depth to handle nested elements
  private dragCounter = 0;

  // Output events
  fileAttached = output<File>();
  messageSubmitted = output<Message>();
  messageCancelled = output<void>();
  settingsToggled = output<void>();

  // File upload state from service
  readonly pendingUploads = this.fileUploadService.pendingUploadsList;
  readonly hasActivePendingUploads = this.fileUploadService.hasActivePendingUploads;
  readonly readyUploadIds = this.fileUploadService.readyUploadIds;

  // Computed: show file attachments area
  readonly showFileAttachments = computed(() => this.pendingUploads().length > 0);

  // Computed: can submit (has content or ready files)
  readonly canSubmit = computed(() => {
    const hasText = this.userInput().trim().length > 0;
    const hasReadyFiles = this.readyUploadIds().length > 0;
    const isUploading = this.hasActivePendingUploads();
    return (hasText || hasReadyFiles) && !isUploading;
  });

  // Allowed file types for input accept attribute
  readonly acceptedFileTypes = ALLOWED_EXTENSIONS.join(',');

  onSubmit() {
    if(this.chatState.isChatLoading()) {
      this.cancelChatRequest();
    } else {
      this.submitChatRequest();
    }
  }

  submitChatRequest() {
    const content = this.userInput().trim();
    const fileUploadIds = this.readyUploadIds();

    // Must have content or files to submit
    if (!content && fileUploadIds.length === 0) {
      return;
    }

    // Don't submit while uploads are in progress
    if (this.hasActivePendingUploads()) {
      this.toastService.warning('Upload in Progress', 'Please wait for file uploads to complete.');
      return;
    }

    this.chatState.setChatLoading(true);
    this.messageSubmitted.emit({
      content,
      timestamp: new Date(),
      fileUploadIds: fileUploadIds.length > 0 ? fileUploadIds : undefined
    });

    // Clear input and pending uploads
    this.userInput.set('');
    this.isExpanded.set(false);
    this.fileUploadService.clearReadyUploads();
  }

  cancelChatRequest() {
    this.messageCancelled.emit();
  }

  toggleSettings() {
    this.settingsToggled.emit();
  }

  async onFileSelect(event: Event) {
    const input = event.target as HTMLInputElement;
    if (!input.files || input.files.length === 0) {
      return;
    }

    await this.processFiles(Array.from(input.files));

    // Reset input to allow re-selecting same file
    input.value = '';
  }

  /**
   * Handle file removal from pending uploads
   */
  onFileRemove(uploadId: string): void {
    this.fileUploadService.clearPendingUpload(uploadId);
  }

  /**
   * Handle retry for failed uploads
   */
  async onFileRetry(pendingUpload: PendingUpload): Promise<void> {
    const sessionId = this.sessionId();
    if (!sessionId) {
      return;
    }

    // Clear the failed upload
    this.fileUploadService.clearPendingUpload(pendingUpload.uploadId);

    // Retry the upload
    try {
      await this.fileUploadService.uploadFile(sessionId, pendingUpload.file);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Retry failed';
      this.toastService.error('Retry Failed', message);
    }
  }

  onTextareaInput(event: Event) {
    const textarea = event.target as HTMLTextAreaElement;
    this.userInput.set(textarea.value);
    
    // Auto-expand based on content
    textarea.style.height = 'auto';
    textarea.style.height = textarea.scrollHeight + 'px';
  }

  onKeyDown(event: KeyboardEvent) {
    // Submit on Enter (without Shift)
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      this.onSubmit();
    }
  }

  onFocus() {
    this.isFocused.set(true);
  }

  onBlur() {
    this.isFocused.set(false);
  }

  // =========================================================================
  // Drag and Drop Handlers
  // =========================================================================

  onDragEnter(event: DragEvent): void {
    event.preventDefault();
    event.stopPropagation();
    this.dragCounter++;

    // Check if dragging files
    if (event.dataTransfer?.types.includes('Files')) {
      this.isDraggingOver.set(true);
    }
  }

  onDragOver(event: DragEvent): void {
    event.preventDefault();
    event.stopPropagation();

    // Set the drop effect
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = 'copy';
    }
  }

  onDragLeave(event: DragEvent): void {
    event.preventDefault();
    event.stopPropagation();
    this.dragCounter--;

    // Only hide overlay when truly leaving the dropzone
    if (this.dragCounter === 0) {
      this.isDraggingOver.set(false);
    }
  }

  async onDrop(event: DragEvent): Promise<void> {
    event.preventDefault();
    event.stopPropagation();

    // Reset drag state
    this.dragCounter = 0;
    this.isDraggingOver.set(false);

    const files = event.dataTransfer?.files;
    if (!files || files.length === 0) {
      return;
    }

    // Process dropped files using the same logic as file select
    await this.processFiles(Array.from(files));
  }

  /**
   * Process files for upload (shared by file input and drag-drop)
   */
  private async processFiles(newFiles: File[]): Promise<void> {
    // Emit fileAttached for each file FIRST to trigger session creation if needed
    for (const file of newFiles) {
      this.fileAttached.emit(file);
    }

    // Wait a tick for Angular to process the signal update from parent
    await new Promise(resolve => setTimeout(resolve, 0));

    // Now get the session ID (should be available after parent creates staged session)
    const sessionId = this.sessionId();
    if (!sessionId) {
      this.toastService.error('Upload Error', 'Failed to create session for file upload.');
      return;
    }

    // Check file count limit
    const currentCount = this.pendingUploads().length;
    if (currentCount + newFiles.length > MAX_FILES_PER_MESSAGE) {
      this.toastService.warning(
        'File Limit',
        `Maximum ${MAX_FILES_PER_MESSAGE} files per message. You have ${currentCount} already attached.`
      );
      return;
    }

    // Validate and upload each file
    for (const file of newFiles) {
      // Check file size
      if (file.size > MAX_FILE_SIZE_BYTES) {
        this.toastService.error(
          'File Too Large',
          `${file.name} exceeds maximum size of ${formatBytes(MAX_FILE_SIZE_BYTES)}.`
        );
        continue;
      }

      // Check file type
      const ext = file.name.substring(file.name.lastIndexOf('.')).toLowerCase();
      if (!ALLOWED_EXTENSIONS.includes(ext)) {
        this.toastService.error(
          'Invalid File Type',
          `${file.name} is not a supported file type. Allowed: ${ALLOWED_EXTENSIONS.join(', ')}`
        );
        continue;
      }

      // Upload file
      try {
        await this.fileUploadService.uploadFile(sessionId, file);
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Upload failed';
        this.toastService.error('Upload Failed', `${file.name}: ${message}`);
      }
    }
  }
}