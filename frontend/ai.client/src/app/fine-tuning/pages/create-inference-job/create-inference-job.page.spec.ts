import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { signal } from '@angular/core';
import { of } from 'rxjs';
import { CreateInferenceJobPage } from './create-inference-job.page';
import { FineTuningStateService } from '../../services/fine-tuning-state.service';
import { FineTuningHttpService } from '../../services/fine-tuning-http.service';
import { FineTuningUploadService } from '../../services/fine-tuning-upload.service';
import type {
  TrainedModelResponse,
  InferenceJobResponse,
  PresignResponse,
} from '../../models/fine-tuning.models';

const mockTrainedModel: TrainedModelResponse = {
  training_job_id: 'tj-1',
  model_id: 'model-1',
  model_name: 'Test Model',
  model_s3_path: 's3://bucket/model',
  instance_type: 'ml.g5.xlarge',
  completed_at: '2026-02-28T00:00:00Z',
  estimated_cost_usd: 10.0,
};

const mockPresignResponse: PresignResponse = {
  presigned_url: 'https://s3.example.com/upload?signed=true',
  s3_key: 'uploads/input.txt',
  expires_at: '2026-03-01T01:00:00Z',
};

const mockInferenceJob: InferenceJobResponse = {
  job_id: 'ij-new',
  user_id: 'u1',
  email: 'test@example.com',
  job_type: 'BATCH_TRANSFORM',
  training_job_id: 'tj-1',
  model_name: 'Test Model',
  model_s3_path: 's3://bucket/model',
  status: 'PENDING',
  input_s3_key: 'uploads/input.txt',
  output_s3_prefix: null,
  result_s3_key: null,
  instance_type: 'ml.g5.xlarge',
  transform_job_name: null,
  transform_start_time: null,
  transform_end_time: null,
  billable_seconds: null,
  estimated_cost_usd: null,
  created_at: '2026-03-01T00:00:00Z',
  updated_at: '2026-03-01T00:00:00Z',
  error_message: null,
  max_runtime_seconds: 3600,
};

function createMockState() {
  return {
    loading: signal(false),
    error: signal<string | null>(null),
    trainedModels: signal<TrainedModelResponse[]>([mockTrainedModel]),
    loadTrainedModels: vi.fn().mockResolvedValue(undefined),
    createInferenceJob: vi.fn().mockResolvedValue(mockInferenceJob),
    clearError: vi.fn(),
  };
}

function createMockHttp() {
  return {
    presignInferenceUpload: vi.fn().mockReturnValue(of(mockPresignResponse)),
  };
}

function createMockUpload() {
  return {
    uploadFile: vi.fn().mockResolvedValue(undefined),
  };
}

describe('CreateInferenceJobPage', () => {
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
    TestBed.overrideComponent(CreateInferenceJobPage, {
      set: { template: '<div></div>' },
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  function createComponent() {
    const fixture = TestBed.createComponent(CreateInferenceJobPage);
    fixture.detectChanges();
    return fixture.componentInstance;
  }

  it('should load trained models on init', () => {
    createComponent();
    expect(mockState.loadTrainedModels).toHaveBeenCalled();
  });

  it('should have default form values', () => {
    const component = createComponent();
    const values = component.form.getRawValue();
    expect(values.trainingJobId).toBe('');
    expect(values.maxRuntimeHours).toBe(1);
  });

  it('should handle file selection and upload', async () => {
    const component = createComponent();
    const file = new File(['line1\nline2'], 'input.txt', { type: 'text/plain' });
    const input = { target: { files: [file], value: 'input.txt' } } as unknown as Event;

    await component.onFileSelected(input);

    expect(mockHttp.presignInferenceUpload).toHaveBeenCalledWith({
      filename: 'input.txt',
      content_type: 'text/plain',
    });
    expect(mockUpload.uploadFile).toHaveBeenCalled();
    expect(component.uploadState()?.status).toBe('complete');
    expect(component.uploadState()?.s3Key).toBe('uploads/input.txt');
  });

  it('should handle upload error', async () => {
    const component = createComponent();
    mockUpload.uploadFile.mockRejectedValueOnce(new Error('Upload failed'));
    const file = new File(['data'], 'input.txt', { type: 'text/plain' });
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
      file: new File([''], 'input.txt'),
      progress: 100,
      status: 'complete',
      s3Key: 'uploads/input.txt',
    });
    component.clearUpload();
    expect(component.uploadState()).toBeNull();
  });

  it('should error when submitting without training job selection', async () => {
    const component = createComponent();
    component.uploadState.set({
      file: new File([''], 'input.txt'),
      progress: 100,
      status: 'complete',
      s3Key: 'uploads/input.txt',
    });
    await component.submitJob();
    expect(component.submitError()).toBe('Please select a trained model.');
  });

  it('should error when submitting without file upload', async () => {
    const component = createComponent();
    component.form.patchValue({ trainingJobId: 'tj-1' });
    await component.submitJob();
    expect(component.submitError()).toBe('Please upload an input file first.');
  });

  it('should submit job and navigate to dashboard', async () => {
    const component = createComponent();
    const router = TestBed.inject(Router);
    const navSpy = vi.spyOn(router, 'navigate').mockResolvedValue(true);

    component.form.patchValue({ trainingJobId: 'tj-1' });
    component.uploadState.set({
      file: new File([''], 'input.txt'),
      progress: 100,
      status: 'complete',
      s3Key: 'uploads/input.txt',
    });

    await component.submitJob();

    expect(mockState.createInferenceJob).toHaveBeenCalledWith(
      expect.objectContaining({
        training_job_id: 'tj-1',
        input_s3_key: 'uploads/input.txt',
      }),
    );
    expect(navSpy).toHaveBeenCalledWith(['/fine-tuning']);
    expect(component.submitting()).toBe(false);
  });

  it('should set submit error on job creation failure', async () => {
    const component = createComponent();
    mockState.createInferenceJob.mockRejectedValueOnce(new Error('Creation failed'));

    component.form.patchValue({ trainingJobId: 'tj-1' });
    component.uploadState.set({
      file: new File([''], 'input.txt'),
      progress: 100,
      status: 'complete',
      s3Key: 'uploads/input.txt',
    });

    await component.submitJob();

    expect(component.submitError()).toBe('Creation failed');
    expect(component.submitting()).toBe(false);
  });

  it('should convert max runtime hours to seconds', async () => {
    const component = createComponent();
    const router = TestBed.inject(Router);
    vi.spyOn(router, 'navigate').mockResolvedValue(true);

    component.form.patchValue({ trainingJobId: 'tj-1', maxRuntimeHours: 2 });
    component.uploadState.set({
      file: new File([''], 'input.txt'),
      progress: 100,
      status: 'complete',
      s3Key: 'uploads/input.txt',
    });

    await component.submitJob();

    const call = mockState.createInferenceJob.mock.calls[0][0];
    expect(call.max_runtime_seconds).toBe(2 * 3600);
  });

  it('should set generic error for non-Error throws', async () => {
    const component = createComponent();
    mockState.createInferenceJob.mockRejectedValueOnce('unknown error');

    component.form.patchValue({ trainingJobId: 'tj-1' });
    component.uploadState.set({
      file: new File([''], 'input.txt'),
      progress: 100,
      status: 'complete',
      s3Key: 'uploads/input.txt',
    });

    await component.submitJob();

    expect(component.submitError()).toBe('Failed to create inference job');
  });
});
