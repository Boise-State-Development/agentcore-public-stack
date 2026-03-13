import { Component, ChangeDetectionStrategy, inject, OnInit, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { ReactiveFormsModule, FormBuilder, Validators } from '@angular/forms';
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
import { AvailableModel, FileUploadState, CreateJobRequest } from '../../models/fine-tuning.models';

@Component({
  selector: 'app-create-training-job',
  imports: [RouterLink, ReactiveFormsModule, NgIcon],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroCloudArrowUp,
      heroCheck,
      heroXMark,
      heroExclamationTriangle,
    }),
  ],
  templateUrl: './create-training-job.page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block' },
})
export class CreateTrainingJobPage implements OnInit {
  private readonly fb = inject(FormBuilder);
  private readonly router = inject(Router);
  private readonly http = inject(FineTuningHttpService);
  private readonly uploadService = inject(FineTuningUploadService);
  readonly state = inject(FineTuningStateService);

  /** Upload state tracking. */
  readonly uploadState = signal<FileUploadState | null>(null);

  /** Currently selected model. */
  readonly selectedModel = signal<AvailableModel | null>(null);

  /** Whether the form is being submitted. */
  readonly submitting = signal(false);

  /** Submit error message. */
  readonly submitError = signal<string | null>(null);

  /** Reactive form for hyperparameters and runtime config. */
  readonly form = this.fb.group({
    epochs: ['3'],
    batchSize: ['4'],
    learningRate: ['2e-5'],
    weightDecay: ['0.01'],
    splitRatio: ['0.8'],
    seed: ['42'],
    contextLength: ['512'],
    maxRuntimeHours: [24, [Validators.required, Validators.min(1), Validators.max(120)]],
  });

  ngOnInit(): void {
    this.state.loadAvailableModels();
  }

  /** Handle file selection from the file input. */
  async onFileSelected(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;

    this.uploadState.set({ file, progress: 0, status: 'uploading' });
    this.submitError.set(null);

    try {
      // Step 1: Get presigned URL
      const presignResponse = await firstValueFrom(
        this.http.presignDatasetUpload({
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

  /** Select a base model and populate hyperparameter defaults. */
  selectModel(model: AvailableModel): void {
    this.selectedModel.set(model);
    const hp = model.default_hyperparameters;
    this.form.patchValue({
      epochs: hp['epochs'] ?? '3',
      batchSize: hp['per_device_train_batch_size'] ?? '4',
      learningRate: hp['learning_rate'] ?? '2e-5',
      weightDecay: hp['weight_decay'] ?? '0.01',
      splitRatio: hp['split_ratio'] ?? '0.8',
      seed: hp['seed'] ?? '42',
      contextLength: hp['context_length'] ?? '512',
    });
  }

  /** Submit the training job. */
  async submitJob(): Promise<void> {
    const upload = this.uploadState();
    const model = this.selectedModel();

    if (!upload?.s3Key) {
      this.submitError.set('Please upload a dataset file first.');
      return;
    }
    if (!model) {
      this.submitError.set('Please select a base model.');
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

      // Build hyperparameters dict from form values (skip empty)
      const hyperparameters: Record<string, string> = {};
      if (formValues.epochs) hyperparameters['epochs'] = formValues.epochs;
      if (formValues.batchSize) hyperparameters['per_device_train_batch_size'] = formValues.batchSize;
      if (formValues.learningRate) hyperparameters['learning_rate'] = formValues.learningRate;
      if (formValues.weightDecay) hyperparameters['weight_decay'] = formValues.weightDecay;
      if (formValues.splitRatio) hyperparameters['split_ratio'] = formValues.splitRatio;
      if (formValues.seed) hyperparameters['seed'] = formValues.seed;
      if (formValues.contextLength) hyperparameters['context_length'] = formValues.contextLength;

      const request: CreateJobRequest = {
        model_id: model.model_id,
        dataset_s3_key: upload.s3Key,
        instance_type: model.default_instance_type,
        hyperparameters,
        max_runtime_seconds: (formValues.maxRuntimeHours ?? 24) * 3600,
      };

      await this.state.createTrainingJob(request);
      this.router.navigate(['/fine-tuning']);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to create training job';
      this.submitError.set(message);
    } finally {
      this.submitting.set(false);
    }
  }
}
