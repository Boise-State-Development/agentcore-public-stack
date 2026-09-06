import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { signal } from '@angular/core';
import { FineTuningDashboardPage } from './fine-tuning-dashboard.page';
import { FineTuningStateService } from '../../services/fine-tuning-state.service';
import type { JobResponse, InferenceJobResponse, FineTuningAccessResponse } from '../../models/fine-tuning.models';

const mockAccess: FineTuningAccessResponse = {
  has_access: true,
  monthly_quota_usd: 10,
  current_month_usage_usd: 3,
  quota_period: '2026-03',
};

const mockTrainingJob: JobResponse = {
  job_id: 'tj-1',
  user_id: 'u1',
  email: 'test@example.com',
  model_id: 'model-1',
  model_name: 'Test Model',
  task_type: 'text-classification',
  status: 'TRAINING',
  dataset_s3_key: 's3://bucket/data.jsonl',
  output_s3_prefix: null,
  instance_type: 'ml.g5.xlarge',
  instance_count: 1,
  hyperparameters: null,
  sagemaker_job_name: null,
  training_start_time: null,
  training_end_time: null,
  billable_seconds: null,
  estimated_cost_usd: 12.5,
  created_at: '2026-03-01T00:00:00Z',
  updated_at: '2026-03-01T00:00:00Z',
  error_message: null,
  max_runtime_seconds: 86400,
  training_progress: null,
};

const mockInferenceJob: InferenceJobResponse = {
  job_id: 'ij-1',
  user_id: 'u1',
  email: 'test@example.com',
  job_type: 'BATCH_TRANSFORM',
  training_job_id: 'tj-1',
  model_name: 'Test Model',
  model_s3_path: 's3://bucket/model',
  status: 'TRANSFORMING',
  input_s3_key: 's3://bucket/input.jsonl',
  output_s3_prefix: null,
  result_s3_key: null,
  instance_type: 'ml.g5.xlarge',
  transform_job_name: null,
  transform_start_time: null,
  transform_end_time: null,
  billable_seconds: null,
  estimated_cost_usd: 5.0,
  created_at: '2026-03-02T00:00:00Z',
  updated_at: '2026-03-02T00:00:00Z',
  error_message: null,
  max_runtime_seconds: 3600,
};

function createMockState() {
  return {
    access: signal<FineTuningAccessResponse | null>(mockAccess),
    hasAccess: signal(true),
    loading: signal(false),
    error: signal<string | null>(null),
    trainingJobs: signal<JobResponse[]>([mockTrainingJob]),
    inferenceJobs: signal<InferenceJobResponse[]>([mockInferenceJob]),
    trainingJobCount: signal(1),
    inferenceJobCount: signal(1),
    loadDashboard: vi.fn().mockResolvedValue(undefined),
    stopTrainingJob: vi.fn().mockResolvedValue(undefined),
    stopInferenceJob: vi.fn().mockResolvedValue(undefined),
    getTrainingDownloadUrl: vi.fn().mockResolvedValue({ download_url: 'https://s3.example.com/artifact', expires_at: '2026-03-01T02:00:00Z' }),
    getInferenceDownloadUrl: vi.fn().mockResolvedValue({ download_url: 'https://s3.example.com/results', expires_at: '2026-03-02T02:00:00Z' }),
    clearError: vi.fn(),
  };
}

describe('FineTuningDashboardPage', () => {
  let mockState: ReturnType<typeof createMockState>;

  beforeEach(() => {
    mockState = createMockState();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: FineTuningStateService, useValue: mockState },
      ],
    });
    TestBed.overrideComponent(FineTuningDashboardPage, {
      set: { template: '<div></div>' },
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  function createComponent() {
    const fixture = TestBed.createComponent(FineTuningDashboardPage);
    fixture.detectChanges();
    return fixture.componentInstance;
  }

  it('should call loadDashboard on init', () => {
    createComponent();
    expect(mockState.loadDashboard).toHaveBeenCalled();
  });

  it('should navigate to new training job', () => {
    const component = createComponent();
    const router = TestBed.inject(Router);
    const spy = vi.spyOn(router, 'navigate').mockResolvedValue(true);
    component.navigateToNewTrainingJob();
    expect(spy).toHaveBeenCalledWith(['/fine-tuning/new-training']);
  });

  it('should navigate to new inference job', () => {
    const component = createComponent();
    const router = TestBed.inject(Router);
    const spy = vi.spyOn(router, 'navigate').mockResolvedValue(true);
    component.navigateToNewInferenceJob();
    expect(spy).toHaveBeenCalledWith(['/fine-tuning/new-inference']);
  });

  it('should set confirming stop training job ID', () => {
    const component = createComponent();
    component.confirmStopTraining('tj-1');
    expect(component.confirmingStopTraining()).toBe('tj-1');
  });

  it('should cancel stop training confirmation', () => {
    const component = createComponent();
    component.confirmStopTraining('tj-1');
    component.cancelStopTraining();
    expect(component.confirmingStopTraining()).toBeNull();
  });

  it('should execute stop training and clear confirmation', async () => {
    const component = createComponent();
    component.confirmStopTraining('tj-1');
    await component.executeStopTraining('tj-1');
    expect(component.confirmingStopTraining()).toBeNull();
    expect(mockState.stopTrainingJob).toHaveBeenCalledWith('tj-1');
  });

  it('should set confirming stop inference job ID', () => {
    const component = createComponent();
    component.confirmStopInference('ij-1');
    expect(component.confirmingStopInference()).toBe('ij-1');
  });

  it('should cancel stop inference confirmation', () => {
    const component = createComponent();
    component.confirmStopInference('ij-1');
    component.cancelStopInference();
    expect(component.confirmingStopInference()).toBeNull();
  });

  it('should execute stop inference and clear confirmation', async () => {
    const component = createComponent();
    component.confirmStopInference('ij-1');
    await component.executeStopInference('ij-1');
    expect(component.confirmingStopInference()).toBeNull();
    expect(mockState.stopInferenceJob).toHaveBeenCalledWith('ij-1');
  });

  it('should call loadDashboard on refresh', async () => {
    const component = createComponent();
    mockState.loadDashboard.mockClear();
    await component.refresh();
    expect(mockState.loadDashboard).toHaveBeenCalled();
  });

  it('should format cost as USD', () => {
    const component = createComponent();
    expect(component.formatCost(12.5)).toBe('$12.50');
    expect(component.formatCost(0)).toBe('$0.00');
  });

  it('should return dash for null cost', () => {
    const component = createComponent();
    expect(component.formatCost(null)).toBe('—');
  });

  it('should identify stoppable training statuses', () => {
    const component = createComponent();
    expect(component.canStopTraining('PENDING')).toBe(true);
    expect(component.canStopTraining('TRAINING')).toBe(true);
    expect(component.canStopTraining('COMPLETED')).toBe(false);
    expect(component.canStopTraining('FAILED')).toBe(false);
    expect(component.canStopTraining('STOPPED')).toBe(false);
  });

  it('should identify stoppable inference statuses', () => {
    const component = createComponent();
    expect(component.canStopInference('PENDING')).toBe(true);
    expect(component.canStopInference('TRANSFORMING')).toBe(true);
    expect(component.canStopInference('COMPLETED')).toBe(false);
    expect(component.canStopInference('FAILED')).toBe(false);
    expect(component.canStopInference('STOPPED')).toBe(false);
  });

  // ── Elapsed timer ─────────────────────────────────────────────────

  it('should initialize now signal with current timestamp', () => {
    const before = Date.now();
    const component = createComponent();
    const after = Date.now();
    expect(component.now()).toBeGreaterThanOrEqual(before);
    expect(component.now()).toBeLessThanOrEqual(after);
  });

  it('should format duration correctly', () => {
    const component = createComponent();
    expect(component.formatDuration(0)).toBe('0s');
    expect(component.formatDuration(45)).toBe('45s');
    expect(component.formatDuration(90)).toBe('1m 30s');
    expect(component.formatDuration(3661)).toBe('1h 1m 1s');
  });

  it('should return elapsed string for active training job', () => {
    const component = createComponent();
    const activeJob: JobResponse = {
      ...mockTrainingJob,
      status: 'TRAINING',
      training_start_time: new Date(Date.now() - 120_000).toISOString(),
    };
    const elapsed = component.getElapsedTraining(activeJob);
    expect(elapsed).toMatch(/\d+m \d+s|\d+s/);
  });

  it('should return empty string for completed training job', () => {
    const component = createComponent();
    const completedJob: JobResponse = { ...mockTrainingJob, status: 'COMPLETED' };
    expect(component.getElapsedTraining(completedJob)).toBe('');
  });

  it('should return elapsed string for active inference job', () => {
    const component = createComponent();
    const activeJob: InferenceJobResponse = {
      ...mockInferenceJob,
      status: 'TRANSFORMING',
      transform_start_time: new Date(Date.now() - 60_000).toISOString(),
    };
    const elapsed = component.getElapsedInference(activeJob);
    expect(elapsed).toMatch(/\d+m \d+s|\d+s/);
  });

  it('should return empty string for completed inference job', () => {
    const component = createComponent();
    const completedJob: InferenceJobResponse = { ...mockInferenceJob, status: 'COMPLETED' };
    expect(component.getElapsedInference(completedJob)).toBe('');
  });

  it('should use created_at as fallback when start time is null', () => {
    const component = createComponent();
    const activeJob: JobResponse = {
      ...mockTrainingJob,
      status: 'PENDING',
      training_start_time: null,
      created_at: new Date(Date.now() - 30_000).toISOString(),
    };
    const elapsed = component.getElapsedTraining(activeJob);
    expect(elapsed).toMatch(/\d+s/);
  });

  // ── Download actions ──────────────────────────────────────────────

  it('should download training artifact and open URL', async () => {
    const component = createComponent();
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    await component.downloadTrainingArtifact('tj-1');
    expect(mockState.getTrainingDownloadUrl).toHaveBeenCalledWith('tj-1');
    expect(openSpy).toHaveBeenCalledWith('https://s3.example.com/artifact', '_blank');
    openSpy.mockRestore();
  });

  it('should set error on training download failure', async () => {
    const component = createComponent();
    mockState.getTrainingDownloadUrl.mockRejectedValueOnce(new Error('fail'));
    await component.downloadTrainingArtifact('tj-1');
    expect(mockState.error()).toBe('Failed to get download URL');
  });

  it('should download inference results and open URL', async () => {
    const component = createComponent();
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    await component.downloadInferenceResults('ij-1');
    expect(mockState.getInferenceDownloadUrl).toHaveBeenCalledWith('ij-1');
    expect(openSpy).toHaveBeenCalledWith('https://s3.example.com/results', '_blank');
    openSpy.mockRestore();
  });

  it('should set error on inference download failure', async () => {
    const component = createComponent();
    mockState.getInferenceDownloadUrl.mockRejectedValueOnce(new Error('fail'));
    await component.downloadInferenceResults('ij-1');
    expect(mockState.error()).toBe('Failed to get download URL');
  });

  // ── Polling ───────────────────────────────────────────────────────

  it('should poll dashboard when active jobs exist', () => {
    vi.useFakeTimers();
    try {
      // mockState has TRAINING and TRANSFORMING jobs by default
      createComponent();
      mockState.loadDashboard.mockClear();

      vi.advanceTimersByTime(10_000);

      expect(mockState.loadDashboard).toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('should stop polling when no active jobs remain', () => {
    vi.useFakeTimers();
    try {
      // Set all jobs to terminal statuses
      mockState.trainingJobs.set([{ ...mockTrainingJob, status: 'COMPLETED' }]);
      mockState.inferenceJobs.set([{ ...mockInferenceJob, status: 'COMPLETED' }]);
      createComponent();
      mockState.loadDashboard.mockClear();

      vi.advanceTimersByTime(10_000);
      vi.advanceTimersByTime(10_000);

      expect(mockState.loadDashboard).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});
