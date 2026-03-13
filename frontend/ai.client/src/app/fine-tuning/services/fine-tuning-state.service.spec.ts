import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { FineTuningStateService } from './fine-tuning-state.service';
import { FineTuningHttpService } from './fine-tuning-http.service';
import type { FineTuningAccessResponse, JobResponse, InferenceJobResponse } from '../models/fine-tuning.models';

describe('FineTuningStateService', () => {
  let service: FineTuningStateService;
  let httpMock: Record<string, ReturnType<typeof vi.fn>>;

  const mockAccess: FineTuningAccessResponse = {
    has_access: true,
    monthly_quota_hours: 10,
    current_month_usage_hours: 3,
    quota_period: '2026-03',
  };

  const mockTrainingJob: JobResponse = {
    job_id: 'j1', user_id: 'u1', email: 'test@example.com', model_id: 'm1',
    model_name: 'Llama', status: 'TRAINING', dataset_s3_key: 'key', output_s3_prefix: null,
    instance_type: 'ml.g5.2xlarge', instance_count: 1, hyperparameters: null,
    sagemaker_job_name: null, training_start_time: null, training_end_time: null,
    billable_seconds: null, estimated_cost_usd: null, created_at: '2026-03-01T00:00:00Z',
    updated_at: '2026-03-01T00:00:00Z', error_message: null, max_runtime_seconds: 86400,
    training_progress: null,
  };

  const mockInferenceJob: InferenceJobResponse = {
    job_id: 'i1', user_id: 'u1', email: 'test@example.com', job_type: 'inference',
    training_job_id: 'j1', model_name: 'Llama', model_s3_path: 's3://bucket/model',
    status: 'TRANSFORMING', input_s3_key: 'input-key', output_s3_prefix: null,
    result_s3_key: null, instance_type: 'ml.g5.2xlarge', transform_job_name: null,
    transform_start_time: null, transform_end_time: null, billable_seconds: null,
    estimated_cost_usd: null, created_at: '2026-03-01T00:00:00Z',
    updated_at: '2026-03-01T00:00:00Z', error_message: null, max_runtime_seconds: 3600,
  };

  beforeEach(() => {
    TestBed.resetTestingModule();

    httpMock = {
      checkAccess: vi.fn(),
      listModels: vi.fn(),
      presignDatasetUpload: vi.fn(),
      createTrainingJob: vi.fn(),
      listTrainingJobs: vi.fn(),
      getTrainingJob: vi.fn(),
      stopTrainingJob: vi.fn(),
      getTrainingJobLogs: vi.fn(),
      downloadTrainingArtifact: vi.fn(),
      listTrainedModels: vi.fn(),
      presignInferenceUpload: vi.fn(),
      createInferenceJob: vi.fn(),
      listInferenceJobs: vi.fn(),
      getInferenceJob: vi.fn(),
      stopInferenceJob: vi.fn(),
      getInferenceJobLogs: vi.fn(),
      downloadInferenceResults: vi.fn(),
    };

    TestBed.configureTestingModule({
      providers: [
        FineTuningStateService,
        { provide: FineTuningHttpService, useValue: httpMock },
      ],
    });

    service = TestBed.inject(FineTuningStateService);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  // ── Initial state ─────────────────────────────────────────────────

  it('should have correct initial state', () => {
    expect(service.access()).toBeNull();
    expect(service.trainingJobs()).toEqual([]);
    expect(service.inferenceJobs()).toEqual([]);
    expect(service.loading()).toBe(false);
    expect(service.error()).toBeNull();
    expect(service.hasAccess()).toBe(false);
    expect(service.quotaUsedPercent()).toBe(0);
    expect(service.trainingJobCount()).toBe(0);
    expect(service.inferenceJobCount()).toBe(0);
    expect(service.currentTrainingJob()).toBeNull();
    expect(service.currentInferenceJob()).toBeNull();
    expect(service.currentLogs()).toEqual([]);
  });

  // ── Computed signals ──────────────────────────────────────────────

  it('should compute hasAccess from access signal', () => {
    service.access.set(mockAccess);
    expect(service.hasAccess()).toBe(true);

    service.access.set({ ...mockAccess, has_access: false });
    expect(service.hasAccess()).toBe(false);
  });

  it('should compute quotaUsedPercent correctly', () => {
    service.access.set(mockAccess); // 3/10 = 30%
    expect(service.quotaUsedPercent()).toBe(30);
  });

  it('should cap quotaUsedPercent at 100', () => {
    service.access.set({ ...mockAccess, current_month_usage_hours: 15, monthly_quota_hours: 10 });
    expect(service.quotaUsedPercent()).toBe(100);
  });

  it('should compute job counts', () => {
    service.trainingJobs.set([mockTrainingJob, { ...mockTrainingJob, job_id: 'j2' }]);
    expect(service.trainingJobCount()).toBe(2);

    service.inferenceJobs.set([mockInferenceJob]);
    expect(service.inferenceJobCount()).toBe(1);
  });

  // ── checkAccess ───────────────────────────────────────────────────

  it('should check access and set access signal', async () => {
    httpMock['checkAccess'].mockReturnValue(of(mockAccess));
    await service.checkAccess();
    expect(service.access()).toEqual(mockAccess);
  });

  it('should set error on checkAccess failure', async () => {
    httpMock['checkAccess'].mockReturnValue(throwError(() => new Error('Denied')));
    await service.checkAccess();
    expect(service.error()).toBe('Denied');
  });

  // ── loadDashboard ─────────────────────────────────────────────────

  it('should load dashboard data when user has access', async () => {
    httpMock['checkAccess'].mockReturnValue(of(mockAccess));
    httpMock['listTrainingJobs'].mockReturnValue(of({ jobs: [mockTrainingJob], total_count: 1 }));
    httpMock['listInferenceJobs'].mockReturnValue(of({ jobs: [mockInferenceJob], total_count: 1 }));

    await service.loadDashboard();

    expect(service.access()).toEqual(mockAccess);
    expect(service.trainingJobs()).toEqual([mockTrainingJob]);
    expect(service.inferenceJobs()).toEqual([mockInferenceJob]);
    expect(service.loading()).toBe(false);
  });

  it('should not load jobs when user has no access', async () => {
    httpMock['checkAccess'].mockReturnValue(of({ ...mockAccess, has_access: false }));

    await service.loadDashboard();

    expect(service.trainingJobs()).toEqual([]);
    expect(service.inferenceJobs()).toEqual([]);
    expect(httpMock['listTrainingJobs']).not.toHaveBeenCalled();
  });

  it('should handle dashboard load error', async () => {
    httpMock['checkAccess'].mockReturnValue(throwError(() => new Error('Network error')));

    await service.loadDashboard();

    expect(service.error()).toBe('Network error');
    expect(service.loading()).toBe(false);
  });

  // ── loadAvailableModels ───────────────────────────────────────────

  it('should load available models', async () => {
    const models = [{ model_id: 'm1', model_name: 'Llama', huggingface_model_id: 'meta', description: 'd', default_instance_type: 'ml.g5.2xlarge', default_hyperparameters: {} }];
    httpMock['listModels'].mockReturnValue(of(models));

    await service.loadAvailableModels();

    expect(service.availableModels()).toEqual(models);
    expect(service.loading()).toBe(false);
  });

  // ── loadTrainedModels ─────────────────────────────────────────────

  it('should load trained models', async () => {
    const trained = [{ training_job_id: 'j1', model_id: 'm1', model_name: 'Llama', model_s3_path: 's3://p', instance_type: 'ml.g5.2xlarge', completed_at: '2026-03-10', estimated_cost_usd: 5.0 }];
    httpMock['listTrainedModels'].mockReturnValue(of(trained));

    await service.loadTrainedModels();

    expect(service.trainedModels()).toEqual(trained);
  });

  // ── createTrainingJob ─────────────────────────────────────────────

  it('should create training job and reload dashboard', async () => {
    const request = { model_id: 'm1', dataset_s3_key: 'key' };
    httpMock['createTrainingJob'].mockReturnValue(of(mockTrainingJob));
    httpMock['checkAccess'].mockReturnValue(of(mockAccess));
    httpMock['listTrainingJobs'].mockReturnValue(of({ jobs: [mockTrainingJob], total_count: 1 }));
    httpMock['listInferenceJobs'].mockReturnValue(of({ jobs: [], total_count: 0 }));

    const result = await service.createTrainingJob(request);

    expect(result).toEqual(mockTrainingJob);
    expect(httpMock['createTrainingJob']).toHaveBeenCalledWith(request);
  });

  it('should throw and set error on createTrainingJob failure', async () => {
    httpMock['createTrainingJob'].mockReturnValue(throwError(() => new Error('Create failed')));

    await expect(service.createTrainingJob({ model_id: 'm1', dataset_s3_key: 'key' })).rejects.toThrow();
    expect(service.error()).toBe('Create failed');
  });

  // ── createInferenceJob ────────────────────────────────────────────

  it('should create inference job and reload dashboard', async () => {
    const request = { training_job_id: 'j1', input_s3_key: 'key' };
    httpMock['createInferenceJob'].mockReturnValue(of(mockInferenceJob));
    httpMock['checkAccess'].mockReturnValue(of(mockAccess));
    httpMock['listTrainingJobs'].mockReturnValue(of({ jobs: [], total_count: 0 }));
    httpMock['listInferenceJobs'].mockReturnValue(of({ jobs: [mockInferenceJob], total_count: 1 }));

    const result = await service.createInferenceJob(request);

    expect(result).toEqual(mockInferenceJob);
  });

  // ── stopTrainingJob ───────────────────────────────────────────────

  it('should stop training job and reload dashboard', async () => {
    httpMock['stopTrainingJob'].mockReturnValue(of({ ...mockTrainingJob, status: 'STOPPED' }));
    httpMock['checkAccess'].mockReturnValue(of(mockAccess));
    httpMock['listTrainingJobs'].mockReturnValue(of({ jobs: [], total_count: 0 }));
    httpMock['listInferenceJobs'].mockReturnValue(of({ jobs: [], total_count: 0 }));

    await service.stopTrainingJob('j1');

    expect(httpMock['stopTrainingJob']).toHaveBeenCalledWith('j1');
    expect(service.error()).toBeNull();
  });

  // ── stopInferenceJob ──────────────────────────────────────────────

  it('should stop inference job and reload dashboard', async () => {
    httpMock['stopInferenceJob'].mockReturnValue(of({ ...mockInferenceJob, status: 'STOPPED' }));
    httpMock['checkAccess'].mockReturnValue(of(mockAccess));
    httpMock['listTrainingJobs'].mockReturnValue(of({ jobs: [], total_count: 0 }));
    httpMock['listInferenceJobs'].mockReturnValue(of({ jobs: [], total_count: 0 }));

    await service.stopInferenceJob('i1');

    expect(httpMock['stopInferenceJob']).toHaveBeenCalledWith('i1');
  });

  // ── Detail page: loadTrainingJobDetail ────────────────────────────

  it('should load training job detail', async () => {
    httpMock['getTrainingJob'].mockReturnValue(of(mockTrainingJob));

    await service.loadTrainingJobDetail('j1');

    expect(service.currentTrainingJob()).toEqual(mockTrainingJob);
    expect(service.loading()).toBe(false);
  });

  it('should handle training job detail error', async () => {
    httpMock['getTrainingJob'].mockReturnValue(throwError(() => new Error('Not found')));

    await service.loadTrainingJobDetail('j1');

    expect(service.error()).toBe('Not found');
    expect(service.loading()).toBe(false);
  });

  // ── Detail page: loadTrainingJobLogs ──────────────────────────────

  it('should load training job logs', async () => {
    httpMock['getTrainingJobLogs'].mockReturnValue(of({ logs: ['line1', 'line2'] }));

    await service.loadTrainingJobLogs('j1');

    expect(service.currentLogs()).toEqual(['line1', 'line2']);
  });

  it('should silently set empty logs on failure', async () => {
    httpMock['getTrainingJobLogs'].mockReturnValue(throwError(() => new Error('No logs')));

    await service.loadTrainingJobLogs('j1');

    expect(service.currentLogs()).toEqual([]);
  });

  // ── Detail page: getTrainingDownloadUrl ───────────────────────────

  it('should return download URL for training artifact', async () => {
    const mockDownload = { download_url: 'https://s3.example.com', expires_at: '2026-03-14T00:00:00Z' };
    httpMock['downloadTrainingArtifact'].mockReturnValue(of(mockDownload));

    const result = await service.getTrainingDownloadUrl('j1');

    expect(result).toEqual(mockDownload);
  });

  // ── Detail page: loadInferenceJobDetail ───────────────────────────

  it('should load inference job detail', async () => {
    httpMock['getInferenceJob'].mockReturnValue(of(mockInferenceJob));

    await service.loadInferenceJobDetail('i1');

    expect(service.currentInferenceJob()).toEqual(mockInferenceJob);
    expect(service.loading()).toBe(false);
  });

  it('should handle inference job detail error', async () => {
    httpMock['getInferenceJob'].mockReturnValue(throwError(() => new Error('Not found')));

    await service.loadInferenceJobDetail('i1');

    expect(service.error()).toBe('Not found');
  });

  // ── Detail page: loadInferenceJobLogs ─────────────────────────────

  it('should load inference job logs', async () => {
    httpMock['getInferenceJobLogs'].mockReturnValue(of({ logs: ['log-line'] }));

    await service.loadInferenceJobLogs('i1');

    expect(service.currentLogs()).toEqual(['log-line']);
  });

  // ── Detail page: getInferenceDownloadUrl ──────────────────────────

  it('should return download URL for inference results', async () => {
    const mockDownload = { download_url: 'https://s3.example.com/results', expires_at: '2026-03-14T00:00:00Z', result_s3_key: 'out.jsonl' };
    httpMock['downloadInferenceResults'].mockReturnValue(of(mockDownload));

    const result = await service.getInferenceDownloadUrl('i1');

    expect(result).toEqual(mockDownload);
  });

  // ── clearError ────────────────────────────────────────────────────

  it('should clear error', () => {
    service.error.set('some error');
    service.clearError();
    expect(service.error()).toBeNull();
  });
});
