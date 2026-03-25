import { Component, ChangeDetectionStrategy, inject, OnInit, OnDestroy, signal, computed } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { ReactiveFormsModule, FormBuilder, FormControl, Validators } from '@angular/forms';
import { DecimalPipe } from '@angular/common';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroCloudArrowUp,
  heroCheck,
  heroXMark,
  heroExclamationTriangle,
  heroMagnifyingGlass,
  heroChevronRight,
  heroArrowPath,
  heroHeart,
  heroArrowDown,
} from '@ng-icons/heroicons/outline';
import { firstValueFrom, Subject, of } from 'rxjs';
import { debounceTime, distinctUntilChanged, switchMap, takeUntil, filter, tap, catchError } from 'rxjs/operators';
import { FineTuningStateService } from '../../services/fine-tuning-state.service';
import { FineTuningHttpService } from '../../services/fine-tuning-http.service';
import { FineTuningUploadService } from '../../services/fine-tuning-upload.service';
import { AvailableModel, FileUploadState, CreateJobRequest, HuggingFaceModelResult } from '../../models/fine-tuning.models';

@Component({
  selector: 'app-create-training-job',
  imports: [RouterLink, ReactiveFormsModule, NgIcon, DecimalPipe],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroCloudArrowUp,
      heroCheck,
      heroXMark,
      heroExclamationTriangle,
      heroMagnifyingGlass,
      heroChevronRight,
      heroArrowPath,
      heroHeart,
      heroArrowDown,
    }),
  ],
  templateUrl: './create-training-job.page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block' },
})
export class CreateTrainingJobPage implements OnInit, OnDestroy {
  private readonly fb = inject(FormBuilder);
  private readonly router = inject(Router);
  private readonly http = inject(FineTuningHttpService);
  private readonly uploadService = inject(FineTuningUploadService);
  readonly state = inject(FineTuningStateService);

  /** Upload state tracking. */
  readonly uploadState = signal<FileUploadState | null>(null);

  /** Whether the user is dragging a file over the drop zone. */
  readonly isDraggingOver = signal(false);

  /** Track drag enter/leave depth to handle nested elements. */
  private dragCounter = 0;

  /** Currently selected model (catalog or custom). */
  readonly selectedModel = signal<AvailableModel | null>(null);

  /** Whether the user chose "Custom HuggingFace Model". */
  readonly useCustomModel = signal(false);

  /** The user-entered HuggingFace model ID (e.g. "bert-base-multilingual-cased"). */
  readonly customHuggingFaceId = signal('');

  /** The selected HuggingFace model details (from search). */
  readonly selectedHfModel = signal<HuggingFaceModelResult | null>(null);

  /** Whether a model is selected (catalog or valid custom). */
  readonly hasModelSelection = computed(() =>
    this.selectedModel() !== null || (this.useCustomModel() && this.customHuggingFaceId().trim().length > 0),
  );

  /** HuggingFace model search results. */
  readonly hfSearchResults = signal<HuggingFaceModelResult[]>([]);

  /** Whether search is in progress. */
  readonly searchingModels = signal(false);

  /** Whether to filter to compatible models only (default: true). */
  readonly compatibleOnly = signal(true);

  /** Whether the search input is focused. */
  private readonly searchFocused = signal(false);

  /** Whether to show the search results dropdown. */
  readonly showSearchResults = computed(() =>
    this.searchFocused() && this.useCustomModel() &&
    (this.searchingModels() || this.hfSearchResults().length > 0 || this.customHuggingFaceId().trim().length >= 2),
  );

  /** Subject for search input debouncing. */
  private readonly searchSubject = new Subject<string>();
  private readonly destroy$ = new Subject<void>();

  /** Whether the form is being submitted. */
  readonly submitting = signal(false);

  /** Submit error message. */
  readonly submitError = signal<string | null>(null);

  /** Standalone form control for the train/test split slider (70–95%). */
  readonly splitSlider = new FormControl(80, { nonNullable: true });

  /** Tick labels for the slider. */
  readonly splitTicks = [70, 75, 80, 85, 90, 95];

  /** Display value derived from the slider. */
  readonly splitPercent = signal(80);

  /** Reactive form for hyperparameters and runtime config. */
  readonly form = this.fb.group({
    epochs: ['3'],
    batchSize: ['4'],
    learningRate: ['2e-5'],
    weightDecay: ['0.01'],
    seed: ['42'],
    contextLength: ['512'],
    maxRuntimeHours: [24, [Validators.required, Validators.min(1), Validators.max(120)]],
  });

  ngOnInit(): void {
    this.state.loadAvailableModels();
    this.splitSlider.valueChanges.subscribe((v) => this.splitPercent.set(v));

    // Set up debounced HuggingFace model search
    this.searchSubject.pipe(
      debounceTime(300),
      distinctUntilChanged(),
      filter((query) => query.trim().length >= 2),
      tap(() => this.searchingModels.set(true)),
      switchMap((query) =>
        this.http.searchHuggingFaceModels(query, this.compatibleOnly()).pipe(
          catchError(() => of([])),
        ),
      ),
      takeUntil(this.destroy$),
    ).subscribe((results) => {
      this.hfSearchResults.set(results);
      this.searchingModels.set(false);
    });
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  /** Handle file selection from the file input. */
  async onFileSelected(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;

    await this.processFileUpload(file);

    // Reset input so the same file can be re-selected
    input.value = '';
  }

  /** Upload a file to S3 via presigned URL (shared by file input and drag-drop). */
  private async processFileUpload(file: File): Promise<void> {
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
  }

  /** Clear the uploaded file. */
  clearUpload(): void {
    this.uploadState.set(null);
  }

  // =========================================================================
  // Drag and Drop Handlers
  // =========================================================================

  onDragEnter(event: DragEvent): void {
    event.preventDefault();
    event.stopPropagation();
    this.dragCounter++;

    if (event.dataTransfer?.types.includes('Files')) {
      this.isDraggingOver.set(true);
    }
  }

  onDragOver(event: DragEvent): void {
    event.preventDefault();
    event.stopPropagation();

    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = 'copy';
    }
  }

  onDragLeave(event: DragEvent): void {
    event.preventDefault();
    event.stopPropagation();
    this.dragCounter--;

    if (this.dragCounter === 0) {
      this.isDraggingOver.set(false);
    }
  }

  async onDrop(event: DragEvent): Promise<void> {
    event.preventDefault();
    event.stopPropagation();

    this.dragCounter = 0;
    this.isDraggingOver.set(false);

    const file = event.dataTransfer?.files?.[0];
    if (!file) return;

    await this.processFileUpload(file);
  }

  /** Switch to custom HuggingFace model entry. */
  selectCustomModel(): void {
    this.selectedModel.set(null);
    this.useCustomModel.set(true);
    // Set sensible defaults for custom models
    this.form.patchValue({
      epochs: '3',
      batchSize: '8',
      learningRate: '2e-5',
      weightDecay: '0.01',
      seed: '42',
      contextLength: '512',
    });
  }

  /** Update the custom HuggingFace model ID and trigger search. */
  onCustomModelInput(event: Event): void {
    const input = event.target as HTMLInputElement;
    this.customHuggingFaceId.set(input.value);
    this.searchSubject.next(input.value);

    if (input.value.trim().length < 2) {
      this.hfSearchResults.set([]);
      this.searchingModels.set(false);
    }
  }

  /** Show search dropdown when input is focused. */
  onSearchFocus(): void {
    this.searchFocused.set(true);
  }

  /** Hide search dropdown when input loses focus. */
  onSearchBlur(): void {
    // Delay to allow mousedown on results to fire first
    setTimeout(() => this.searchFocused.set(false), 200);
  }

  /** Select a HuggingFace model from search results. */
  selectHfModel(model: HuggingFaceModelResult): void {
    this.customHuggingFaceId.set(model.id);
    this.selectedHfModel.set(model);
    this.hfSearchResults.set([]);
    this.searchFocused.set(false);
  }

  /** Toggle compatible-only filter and re-trigger search. */
  toggleCompatibleOnly(): void {
    this.compatibleOnly.update((v) => !v);
    const query = this.customHuggingFaceId().trim();
    if (query.length >= 2) {
      this.searchSubject.next(query);
    }
  }

  /** Clear the selected HuggingFace model and return to search. */
  clearHfModel(): void {
    this.selectedHfModel.set(null);
    this.customHuggingFaceId.set('');
  }

  /** Select a base model and populate hyperparameter defaults. */
  selectModel(model: AvailableModel): void {
    this.useCustomModel.set(false);
    this.customHuggingFaceId.set('');
    this.selectedHfModel.set(null);
    this.selectedModel.set(model);
    const hp = model.default_hyperparameters;
    this.form.patchValue({
      epochs: hp['epochs'] ?? '3',
      batchSize: hp['per_device_train_batch_size'] ?? '4',
      learningRate: hp['learning_rate'] ?? '2e-5',
      weightDecay: hp['weight_decay'] ?? '0.01',
      seed: hp['seed'] ?? '42',
      contextLength: hp['context_length'] ?? '512',
    });

    // Sync slider from model defaults (e.g. "0.8" → 80)
    const splitStr = hp['split_ratio'] ?? '0.8';
    this.splitSlider.setValue(Math.round(parseFloat(splitStr) * 100));
  }

  /** Submit the training job. */
  async submitJob(): Promise<void> {
    const upload = this.uploadState();
    const model = this.selectedModel();

    const isCustom = this.useCustomModel();
    const customHfId = this.customHuggingFaceId().trim();

    if (!upload?.s3Key) {
      this.submitError.set('Please upload a dataset file first.');
      return;
    }
    if (!model && !isCustom) {
      this.submitError.set('Please select a base model.');
      return;
    }
    if (isCustom && !customHfId) {
      this.submitError.set('Please enter a HuggingFace model ID.');
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
      if (formValues.seed) hyperparameters['seed'] = formValues.seed;
      if (formValues.contextLength) hyperparameters['context_length'] = formValues.contextLength;

      // Convert slider percentage (e.g. 80) to decimal string (e.g. "0.8")
      hyperparameters['split_ratio'] = (this.splitSlider.value / 100).toString();

      const request: CreateJobRequest = {
        model_id: isCustom ? 'custom' : model!.model_id,
        dataset_s3_key: upload.s3Key,
        instance_type: isCustom ? 'ml.g5.xlarge' : model!.default_instance_type,
        hyperparameters,
        max_runtime_seconds: (formValues.maxRuntimeHours ?? 24) * 3600,
        ...(isCustom ? { custom_huggingface_model_id: customHfId } : {}),
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
