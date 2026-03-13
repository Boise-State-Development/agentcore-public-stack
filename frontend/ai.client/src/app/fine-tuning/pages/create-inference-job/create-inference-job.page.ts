import { Component, ChangeDetectionStrategy, inject, OnInit, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { ReactiveFormsModule, FormBuilder, Validators } from '@angular/forms';
import { DatePipe } from '@angular/common';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroCloudArrowUp,
  heroCheck,
  heroXMark,
  heroExclamationTriangle,
} from '@ng-icons/heroicons/outline';
import { firstValueFrom } from 'rxjs';
import { FineTuningStateService } from '../../services/fine-tuning-state.service';
import { FineTuningHttpService } from '../../services/fine-tuning-http.service';
import { FineTuningUploadService } from '../../services/fine-tuning-upload.service';
import { FileUploadState, CreateInferenceJobRequest } from '../../models/fine-tuning.models';

@Component({
  selector: 'app-create-inference-job',
  imports: [RouterLink, ReactiveFormsModule, DatePipe, NgIcon],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroCloudArrowUp,
      heroCheck,
      heroXMark,
      heroExclamationTriangle,
    }),
  ],
  templateUrl: './create-inference-job.page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block' },
})
export class CreateInferenceJobPage implements OnInit {
  private readonly fb = inject(FormBuilder);
  private readonly router = inject(Router);
  private readonly http = inject(FineTuningHttpService);
  private readonly uploadService = inject(FineTuningUploadService);
  readonly state = inject(FineTuningStateService);

  /** Upload state tracking. */
  readonly uploadState = signal<FileUploadState | null>(null);

  /** Whether the form is being submitted. */
  readonly submitting = signal(false);

  /** Submit error message. */
  readonly submitError = signal<string | null>(null);

  /** Reactive form for inference job configuration. */
  readonly form = this.fb.group({
    trainingJobId: ['', Validators.required],
    maxRuntimeHours: [1, [Validators.required, Validators.min(0.1), Validators.max(24)]],
  });

  ngOnInit(): void {
    this.state.loadTrainedModels();
  }

  /** Handle file selection from the file input. */
  async onFileSelected(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;

    this.uploadState.set({ file, progress: 0, status: 'uploading' });
    this.submitError.set(null);

    try {
      // Step 1: Get presigned URL for inference input
      const presignResponse = await firstValueFrom(
        this.http.presignInferenceUpload({
          filename: file.name,
          content_type: file.type || 'application/octet-stream',
        }),
      );

      // Step 2: Upload to S3 with progress
      await this.uploadService.uploadFile(presignResponse.presigned_url, file, (progress) => {
        this.uploadState.update((s) => (s ? { ...s, progress } : null));
      });

      // Step 3: Mark complete
      this.uploadState.set({
        file,
        progress: 100,
        status: 'complete',
        s3Key: presignResponse.s3_key,
      });
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'File upload failed';
      this.uploadState.set({
        file,
        progress: 0,
        status: 'error',
        error: message,
      });
    }

    // Reset input so the same file can be re-selected
    input.value = '';
  }

  /** Clear the uploaded file. */
  clearUpload(): void {
    this.uploadState.set(null);
  }

  /** Submit the inference job. */
  async submitJob(): Promise<void> {
    const upload = this.uploadState();
    const trainingJobId = this.form.get('trainingJobId')?.value;

    if (!trainingJobId) {
      this.submitError.set('Please select a trained model.');
      return;
    }
    if (!upload?.s3Key) {
      this.submitError.set('Please upload an input file first.');
      return;
    }
    if (this.form.invalid) {
      this.submitError.set('Please fix the form errors before submitting.');
      return;
    }

    this.submitting.set(true);
    this.submitError.set(null);

    try {
      const formValues = this.form.getRawValue();

      const request: CreateInferenceJobRequest = {
        training_job_id: formValues.trainingJobId ?? '',
        input_s3_key: upload.s3Key,
        max_runtime_seconds: (formValues.maxRuntimeHours ?? 1) * 3600,
      };

      await this.state.createInferenceJob(request);
      this.router.navigate(['/fine-tuning']);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to create inference job';
      this.submitError.set(message);
    } finally {
      this.submitting.set(false);
    }
  }
}
