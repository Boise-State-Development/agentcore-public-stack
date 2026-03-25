import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { signal } from '@angular/core';
import { of, throwError } from 'rxjs';
import { CreateTrainingJobPage } from './create-training-job.page';
import { FineTuningStateService } from '../../services/fine-tuning-state.service';
import { FineTuningHttpService } from '../../services/fine-tuning-http.service';
import { FineTuningUploadService } from '../../services/fine-tuning-upload.service';
import type { AvailableModel, JobResponse, PresignResponse } from '../../models/fine-tuning.models';

const mockModel: AvailableModel = {
  model_id: 'model-1',
  model_name: 'Test Model',
  huggingface_model_id: 'test/model',
  description: 'A test model',
  default_instance_type: 'ml.g5.xlarge',
  default_hyperparameters: {
    epochs: '5',
    per_device_train_batch_size: '8',
    learning_rate: '1e-4',
    weight_decay: '0.02',
    split_ratio: '0.9',
    seed: '123',
    context_length: '1024',
  },
};

const mockPresignResponse: PresignResponse = {
  presigned_url: 'https://s3.example.com/upload?signed=true',
  s3_key: 'uploads/data.jsonl',
  expires_at: '2026-03-01T01:00:00Z',
};

const mockJobResponse: JobResponse = {
  job_id: 'tj-new',
  user_id: 'u1',
  email: 'test@example.com',
  model_id: 'model-1',
  model_name: 'Test Model',
  status: 'PENDING',
  dataset_s3_key: 'uploads/data.jsonl',
  output_s3_prefix: null,
  instance_type: 'ml.g5.xlarge',
  instance_count: 1,
  hyperparameters: null,
  sagemaker_job_name: null,
  training_start_time: null,
  training_end_time: null,
  billable_seconds: null,
  estimated_cost_usd: null,
  created_at: '2026-03-01T00:00:00Z',
  updated_at: '2026-03-01T00:00:00Z',
  error_message: null,
  max_runtime_seconds: 86400,
  training_progress: null,
};

function createMockState() {
  return {
    loading: signal(false),
    error: signal<string | null>(null),
    availableModels: signal<AvailableModel[]>([mockModel]),
    loadAvailableModels: vi.fn().mockResolvedValue(undefined),
    createTrainingJob: vi.fn().mockResolvedValue(mockJobResponse),
    clearError: vi.fn(),
  };
}

function createMockHttp() {
  return {
    presignDatasetUpload: vi.fn().mockReturnValue(of(mockPresignResponse)),
    searchHuggingFaceModels: vi.fn().mockReturnValue(of([])),
  };
}

function createMockUpload() {
  return {
    uploadFile: vi.fn().mockResolvedValue(undefined),
  };
}

describe('CreateTrainingJobPage', () => {
  let mockState: ReturnType<typeof createMockState>;
  let mockHttp: ReturnType<typeof createMockHttp>;
  let mockUpload: ReturnType<typeof createMockUpload>;

  beforeEach(() => {
    mockState = createMockState();
    mockHttp = createMockHttp();
    mockUpload = createMockUpload();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: FineTuningStateService, useValue: mockState },
        { provide: FineTuningHttpService, useValue: mockHttp },
        { provide: FineTuningUploadService, useValue: mockUpload },
      ],
    });
    TestBed.overrideComponent(CreateTrainingJobPage, {
      set: { template: '<div></div>' },
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  function createComponent() {
    const fixture = TestBed.createComponent(CreateTrainingJobPage);
    fixture.detectChanges();
    return fixture.componentInstance;
  }

  it('should load available models on init', () => {
    createComponent();
    expect(mockState.loadAvailableModels).toHaveBeenCalled();
  });

  it('should have default form values', () => {
    const component = createComponent();
    const values = component.form.getRawValue();
    expect(values.epochs).toBe('3');
    expect(values.batchSize).toBe('4');
    expect(values.learningRate).toBe('2e-5');
    expect(values.weightDecay).toBe('0.01');
    expect(values.seed).toBe('42');
    expect(values.contextLength).toBe('512');
    expect(values.maxRuntimeHours).toBe(24);
    expect(component.splitSlider.value).toBe(80);
  });

  it('should select model and populate hyperparameter defaults', () => {
    const component = createComponent();
    component.selectModel(mockModel);
    expect(component.selectedModel()).toBe(mockModel);
    const values = component.form.getRawValue();
    expect(values.epochs).toBe('5');
    expect(values.batchSize).toBe('8');
    expect(values.learningRate).toBe('1e-4');
    expect(values.weightDecay).toBe('0.02');
    expect(values.seed).toBe('123');
    expect(values.contextLength).toBe('1024');
    expect(component.splitSlider.value).toBe(90);
  });

  it('should handle file selection and upload', async () => {
    const component = createComponent();
    const file = new File(['test data'], 'data.jsonl', { type: 'application/jsonl' });
    const input = { target: { files: [file], value: 'data.jsonl' } } as unknown as Event;

    await component.onFileSelected(input);

    expect(mockHttp.presignDatasetUpload).toHaveBeenCalledWith({
      filename: 'data.jsonl',
      content_type: 'application/jsonl',
    });
    expect(mockUpload.uploadFile).toHaveBeenCalled();
    expect(component.uploadState()?.status).toBe('complete');
    expect(component.uploadState()?.s3Key).toBe('uploads/data.jsonl');
  });

  it('should use fallback content type when file.type is empty', async () => {
    const component = createComponent();
    const file = new File(['test data'], 'data.jsonl');
    // File type defaults to '' for unknown extensions
    Object.defineProperty(file, 'type', { value: '' });
    const input = { target: { files: [file], value: 'data.jsonl' } } as unknown as Event;

    await component.onFileSelected(input);

    expect(mockHttp.presignDatasetUpload).toHaveBeenCalledWith({
      filename: 'data.jsonl',
      content_type: 'application/octet-stream',
    });
  });

  it('should handle upload error', async () => {
    const component = createComponent();
    mockUpload.uploadFile.mockRejectedValueOnce(new Error('Upload failed'));
    const file = new File(['test data'], 'data.jsonl', { type: 'application/jsonl' });
    const input = { target: { files: [file], value: '' } } as unknown as Event;

    await component.onFileSelected(input);

    expect(component.uploadState()?.status).toBe('error');
    expect(component.uploadState()?.error).toBe('Upload failed');
  });

  it('should do nothing if no file is selected', async () => {
    const component = createComponent();
    const input = { target: { files: [] } } as unknown as Event;
    await component.onFileSelected(input);
    expect(component.uploadState()).toBeNull();
  });

  it('should clear upload state', () => {
    const component = createComponent();
    component.uploadState.set({
      file: new File([''], 'test.jsonl'),
      progress: 100,
      status: 'complete',
      s3Key: 'uploads/test.jsonl',
    });
    component.clearUpload();
    expect(component.uploadState()).toBeNull();
  });

  it('should error when submitting without upload', async () => {
    const component = createComponent();
    component.selectModel(mockModel);
    await component.submitJob();
    expect(component.submitError()).toBe('Please upload a dataset file first.');
  });

  it('should error when submitting without model selection', async () => {
    const component = createComponent();
    component.uploadState.set({
      file: new File([''], 'test.jsonl'),
      progress: 100,
      status: 'complete',
      s3Key: 'uploads/test.jsonl',
    });
    await component.submitJob();
    expect(component.submitError()).toBe('Please select a base model.');
  });

  it('should submit job and navigate to dashboard', async () => {
    const component = createComponent();
    const router = TestBed.inject(Router);
    const navSpy = vi.spyOn(router, 'navigate').mockResolvedValue(true);

    component.selectModel(mockModel);
    component.uploadState.set({
      file: new File([''], 'test.jsonl'),
      progress: 100,
      status: 'complete',
      s3Key: 'uploads/test.jsonl',
    });

    await component.submitJob();

    expect(mockState.createTrainingJob).toHaveBeenCalledWith(
      expect.objectContaining({
        model_id: 'model-1',
        dataset_s3_key: 'uploads/test.jsonl',
        instance_type: 'ml.g5.xlarge',
      }),
    );
    expect(navSpy).toHaveBeenCalledWith(['/fine-tuning']);
    expect(component.submitting()).toBe(false);
  });

  it('should set submit error on job creation failure', async () => {
    const component = createComponent();
    mockState.createTrainingJob.mockRejectedValueOnce(new Error('Creation failed'));

    component.selectModel(mockModel);
    component.uploadState.set({
      file: new File([''], 'test.jsonl'),
      progress: 100,
      status: 'complete',
      s3Key: 'uploads/test.jsonl',
    });

    await component.submitJob();

    expect(component.submitError()).toBe('Creation failed');
    expect(component.submitting()).toBe(false);
  });

  it('should build hyperparameters dict from form values', async () => {
    const component = createComponent();
    const router = TestBed.inject(Router);
    vi.spyOn(router, 'navigate').mockResolvedValue(true);

    component.selectModel(mockModel);
    component.uploadState.set({
      file: new File([''], 'test.jsonl'),
      progress: 100,
      status: 'complete',
      s3Key: 'uploads/test.jsonl',
    });

    await component.submitJob();

    const call = mockState.createTrainingJob.mock.calls[0][0];
    expect(call.hyperparameters).toEqual(
      expect.objectContaining({
        epochs: '5',
        per_device_train_batch_size: '8',
        learning_rate: '1e-4',
      }),
    );
  });

  it('should convert max runtime hours to seconds', async () => {
    const component = createComponent();
    const router = TestBed.inject(Router);
    vi.spyOn(router, 'navigate').mockResolvedValue(true);

    component.selectModel(mockModel);
    component.form.patchValue({ maxRuntimeHours: 48 });
    component.uploadState.set({
      file: new File([''], 'test.jsonl'),
      progress: 100,
      status: 'complete',
      s3Key: 'uploads/test.jsonl',
    });

    await component.submitJob();

    const call = mockState.createTrainingJob.mock.calls[0][0];
    expect(call.max_runtime_seconds).toBe(48 * 3600);
  });

  // ── Custom HuggingFace Model ─────────────────────────────────────

  it('should enable custom model mode and set defaults', () => {
    const component = createComponent();
    component.selectCustomModel();
    expect(component.useCustomModel()).toBe(true);
    expect(component.selectedModel()).toBeNull();
    const values = component.form.getRawValue();
    expect(values.batchSize).toBe('8');
  });

  it('should clear custom model state when selecting a catalog model', () => {
    const component = createComponent();
    component.selectCustomModel();
    component.customHuggingFaceId.set('some/model');
    component.selectModel(mockModel);
    expect(component.useCustomModel()).toBe(false);
    expect(component.customHuggingFaceId()).toBe('');
    expect(component.selectedHfModel()).toBeNull();
  });

  it('should select HF model from search results', () => {
    const component = createComponent();
    component.selectCustomModel();
    const hfModel = { id: 'bert-base-multilingual-cased', downloads: 5000, likes: 100 };
    component.selectHfModel(hfModel);
    expect(component.customHuggingFaceId()).toBe('bert-base-multilingual-cased');
    expect(component.selectedHfModel()).toEqual(hfModel);
    expect(component.hfSearchResults()).toEqual([]);
  });

  it('should clear HF model selection', () => {
    const component = createComponent();
    component.selectCustomModel();
    component.selectHfModel({ id: 'some/model', downloads: 100, likes: 5 });
    component.clearHfModel();
    expect(component.selectedHfModel()).toBeNull();
    expect(component.customHuggingFaceId()).toBe('');
  });

  it('should toggle compatible-only filter', () => {
    const component = createComponent();
    expect(component.compatibleOnly()).toBe(true);
    component.toggleCompatibleOnly();
    expect(component.compatibleOnly()).toBe(false);
    component.toggleCompatibleOnly();
    expect(component.compatibleOnly()).toBe(true);
  });

  it('should compute hasModelSelection for custom model with ID', () => {
    const component = createComponent();
    component.selectCustomModel();
    expect(component.hasModelSelection()).toBe(false);
    component.customHuggingFaceId.set('bert-base-uncased');
    expect(component.hasModelSelection()).toBe(true);
  });

  it('should error when submitting custom model without HF ID', async () => {
    const component = createComponent();
    component.selectCustomModel();
    component.uploadState.set({
      file: new File([''], 'test.jsonl'),
      progress: 100,
      status: 'complete',
      s3Key: 'uploads/test.jsonl',
    });
    await component.submitJob();
    expect(component.submitError()).toBe('Please enter a HuggingFace model ID.');
  });

  it('should submit custom model job with correct payload', async () => {
    const component = createComponent();
    const router = TestBed.inject(Router);
    vi.spyOn(router, 'navigate').mockResolvedValue(true);

    component.selectCustomModel();
    component.customHuggingFaceId.set('bert-base-multilingual-cased');
    component.uploadState.set({
      file: new File([''], 'test.jsonl'),
      progress: 100,
      status: 'complete',
      s3Key: 'uploads/test.jsonl',
    });

    await component.submitJob();

    const call = mockState.createTrainingJob.mock.calls[0][0];
    expect(call.model_id).toBe('custom');
    expect(call.custom_huggingface_model_id).toBe('bert-base-multilingual-cased');
    expect(call.instance_type).toBe('ml.g5.xlarge');
  });
});
