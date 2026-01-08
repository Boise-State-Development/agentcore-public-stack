import { Component, ChangeDetectionStrategy, inject, signal, computed, OnInit } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { ReactiveFormsModule, FormBuilder, FormGroup, Validators } from '@angular/forms';
import { AssistantService } from '../services/assistant.service';
import { DocumentService, DocumentUploadError } from '../services/document.service';
import { Document } from '../models/document.model';
import { TestChatComponent } from './components/test-chat.component';

@Component({
  selector: 'app-assistant-form-page',
  templateUrl: './assistant-form.page.html',
  styleUrl: './assistant-form.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReactiveFormsModule, TestChatComponent],
})
export class AssistantFormPage implements OnInit {
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  private fb = inject(FormBuilder);
  private assistantService = inject(AssistantService);
  private documentService = inject(DocumentService);

  readonly assistantId = signal<string | null>(null);
  readonly mode = computed<'create' | 'edit'>(() => 
    this.assistantId() ? 'edit' : 'create'
  );

  readonly uploadedDocuments = signal<Document[]>([]);
  readonly currentUpload = signal<{
    file: File;
    progress: number;
    status: 'uploading' | 'complete' | 'error';
    error?: string;
  } | null>(null);
  readonly pollingDocuments = signal<Set<string>>(new Set());

  form!: FormGroup;

  ngOnInit(): void {
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
      status: ['DRAFT']
    });

    // If editing, load the assistant data and documents
    if (id) {
      this.loadAssistant(id);
      this.loadDocuments();
    }
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
          status: assistant.status
        });
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
          status: 'COMPLETE' as const
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
        error: `File size exceeds 10MB limit. File size: ${this.formatBytes(file.size)}`
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
          name: this.form.get('name')?.value || 'Untitled Assistant'
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
          status: draft.status
        });
      } catch (error) {
        const errorMessage = error instanceof Error ? error.message : 'Failed to create assistant';
        this.currentUpload.set({
          file,
          progress: 0,
          status: 'error',
          error: errorMessage
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
      status: 'uploading'
    });

    try {
      // Step 1: Request presigned URL
      const uploadUrlResponse = await this.documentService.requestUploadUrl(assistantId, file);

      // Step 2: Upload to S3 with progress tracking
      await this.documentService.uploadToS3(
        uploadUrlResponse.uploadUrl,
        file,
        (progress) => {
          this.currentUpload.update(current => {
            if (!current) return current;
            return { ...current, progress };
          });
        }
      );

      // Step 3: Mark upload as complete
      this.currentUpload.set({
        file,
        progress: 100,
        status: 'complete'
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
      const errorMessage = error instanceof DocumentUploadError
        ? error.message
        : error instanceof Error
        ? error.message
        : 'Upload failed';
      
      this.currentUpload.set({
        file,
        progress: this.currentUpload()?.progress || 0,
        status: 'error',
        error: errorMessage
      });
    }
  }

  async loadDocuments(): Promise<void> {
    const assistantId = this.assistantId();
    if (!assistantId) {
      return;
    }

    try {
      const response = await this.documentService.listDocuments(assistantId);
      this.uploadedDocuments.set(response.documents);
      
      // Start polling for any documents that are still processing
      const processingStates: Array<'uploading' | 'chunking' | 'embedding'> = ['uploading', 'chunking', 'embedding'];
      for (const doc of response.documents) {
        if (processingStates.includes(doc.status as 'uploading' | 'chunking' | 'embedding')) {
          // Only start polling if not already polling
          if (!this.pollingDocuments().has(doc.documentId)) {
            this.startPollingDocument(doc.documentId, assistantId);
          }
        }
      }
    } catch (error) {
      console.error('Error loading documents:', error);
      // Don't show error to user, just log it
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
    this.pollingDocuments.update(set => new Set(set).add(documentId));

    try {
      await this.documentService.pollDocumentStatus(
        assistantId,
        documentId,
        (document) => {
          // Update the document in the list
          this.uploadedDocuments.update(docs =>
            docs.map(doc =>
              doc.documentId === documentId ? document : doc
            )
          );
        }
      );

      // Polling completed - reload full list to ensure consistency
      await this.loadDocuments();
    } catch (error) {
      console.error('Error polling document status:', error);
      // Reload list anyway to get current state
      await this.loadDocuments();
    } finally {
      // Remove from polling set
      this.pollingDocuments.update(set => {
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

