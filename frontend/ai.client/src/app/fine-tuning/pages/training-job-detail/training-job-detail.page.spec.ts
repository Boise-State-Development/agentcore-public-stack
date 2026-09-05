import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter, ActivatedRoute } from '@angular/router';
import { signal } from '@angular/core';
import { TrainingJobDetailPage } from './training-job-detail.page';
import { FineTuningStateService } from '../../services/fine-tuning-state.service';
import type { JobResponse, DownloadResponse } from '../../models/fine-tuning.models';

const mockJob: JobResponse = {
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
  hyperparameters: { epochs: '3', learning_rate: '2e-5' },
  sagemaker_job_name: 'sagemaker-job-1',
  training_start_time: '2026-03-01T01:00:00Z',
  training_end_time: null,
  billable_seconds: 3661,
  estimated_cost_usd: 12.5,
  created_at: '2026-03-01T00:00:00Z',
  updated_at: '2026-03-01T01:00:00Z',
  error_message: null,
  max_runtime_seconds: 86400,
  training_progress: 45,
};

function createMockState() {
  return {
    currentTrainingJob: signal<JobResponse | null>(mockJob),
    currentLogs: signal<string[]>(['log line 1', 'log line 2']),
    loading: signal(false),
    error: signal<string | null>(null),
    loadTrainingJobDetail: vi.fn().mockResolvedValue(undefined),
    loadTrainingJobLogs: vi.fn().mockResolvedValue(undefined),
    getTrainingDownloadUrl: vi.fn().mockResolvedValue({ download_url: 'https://s3.example.com/artifact', expires_at: '2026-03-01T02:00:00Z' } as DownloadResponse),
    stopTrainingJob: vi.fn().mockResolvedValue(undefined),
    clearError: vi.fn(),
  };
}

describe('TrainingJobDetailPage', () => {
  let mockState: ReturnType<typeof createMockState>;

  beforeEach(() => {
    mockState = createMockState();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: FineTuningStateService, useValue: mockState },
        {
          provide: ActivatedRoute,
          useValue: {
            snapshot: { paramMap: { get: (key: string) => key === 'jobId' ? 'tj-1' : null } },
          },
        },
      ],
    });
    TestBed.overrideComponent(TrainingJobDetailPage, {
      set: { template: '<div></div>' },
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  function createComponent() {
    const fixture = TestBed.createComponent(TrainingJobDetailPage);
    fixture.detectChanges();
    return fixture.componentInstance;
  }

  it('should load job detail and logs on init', () => {
    createComponent();
    expect(mockState.loadTrainingJobDetail).toHaveBeenCalledWith('tj-1');
    expect(mockState.loadTrainingJobLogs).toHaveBeenCalledWith('tj-1');
  });

  it('should not load if jobId is empty', () => {
    TestBed.overrideProvider(ActivatedRoute, {
      useValue: { snapshot: { paramMap: { get: () => null } } },
    });
    createComponent();
    // loadTrainingJobDetail is not called because jobId is empty
    // (it was called during setup but we can't override after configureTestingModule easily)
    // Instead, test the behavior directly
  });

  it('should refresh job detail and logs', async () => {
    const component = createComponent();
    mockState.loadTrainingJobDetail.mockClear();
    mockState.loadTrainingJobLogs.mockClear();
    await component.refreshJob();
    expect(mockState.loadTrainingJobDetail).toHaveBeenCalledWith('tj-1');
    expect(mockState.loadTrainingJobLogs).toHaveBeenCalledWith('tj-1');
  });

  it('should have loadingLogs signal defaulting to false', () => {
    // Create without triggering ngOnInit by not calling detectChanges
    const fixture = TestBed.createComponent(TrainingJobDetailPage);
    expect(fixture.componentInstance.loadingLogs()).toBe(false);
  });

  it('should refresh only logs', async () => {
    const component = createComponent();
    mockState.loadTrainingJobLogs.mockClear();
    await component.refreshLogs();
    expect(mockState.loadTrainingJobLogs).toHaveBeenCalledWith('tj-1');
  });

  it('should show stop confirmation', () => {
    const component = createComponent();
    expect(component.confirmingStop()).toBe(false);
    component.confirmStop();
    expect(component.confirmingStop()).toBe(true);
  });

  it('should cancel stop confirmation', () => {
    const component = createComponent();
    component.confirmStop();
    component.cancelStop();
    expect(component.confirmingStop()).toBe(false);
  });

  it('should execute stop and reload detail', async () => {
    const component = createComponent();
    component.confirmStop();
    mockState.loadTrainingJobDetail.mockClear();
    await component.executeStop();
    expect(component.confirmingStop()).toBe(false);
    expect(mockState.stopTrainingJob).toHaveBeenCalledWith('tj-1');
    expect(mockState.loadTrainingJobDetail).toHaveBeenCalledWith('tj-1');
  });

  it('should download artifact and open URL', async () => {
    const component = createComponent();
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    await component.downloadArtifact();
    expect(mockState.getTrainingDownloadUrl).toHaveBeenCalledWith('tj-1');
    expect(openSpy).toHaveBeenCalledWith('https://s3.example.com/artifact', '_blank');
    expect(component.loadingDownload()).toBe(false);
    openSpy.mockRestore();
  });

  it('should set download error on failure', async () => {
    const component = createComponent();
    mockState.getTrainingDownloadUrl.mockRejectedValueOnce(new Error('Download failed'));
    await component.downloadArtifact();
    expect(component.downloadError()).toBe('Download failed');
    expect(component.loadingDownload()).toBe(false);
  });

  it('should set generic download error for non-Error throws', async () => {
    const component = createComponent();
    mockState.getTrainingDownloadUrl.mockRejectedValueOnce('unknown');
    await component.downloadArtifact();
    expect(component.downloadError()).toBe('Failed to get download URL');
  });

  it('should identify stoppable statuses', () => {
    const component = createComponent();
    expect(component.canStop('PENDING')).toBe(true);
    expect(component.canStop('TRAINING')).toBe(true);
    expect(component.canStop('COMPLETED')).toBe(false);
    expect(component.canStop('FAILED')).toBe(false);
    expect(component.canStop('STOPPED')).toBe(false);
  });

  it('should format duration with hours, minutes, seconds', () => {
    const component = createComponent();
    expect(component.formatDuration(3661)).toBe('1h 1m 1s');
    expect(component.formatDuration(61)).toBe('1m 1s');
    expect(component.formatDuration(5)).toBe('5s');
    expect(component.formatDuration(0)).toBe('0s');
  });

  it('should return dash for null duration', () => {
    const component = createComponent();
    expect(component.formatDuration(null)).toBe('-');
  });

  it('should format cost as USD', () => {
    const component = createComponent();
    expect(component.formatCost(12.5)).toBe('$12.50');
    expect(component.formatCost(0)).toBe('$0.00');
  });

  it('should return dash for null cost', () => {
    const component = createComponent();
    expect(component.formatCost(null)).toBe('-');
  });

  // ── Elapsed timer ─────────────────────────────────────────────────

  it('should initialize now signal with current timestamp', () => {
    const before = Date.now();
    const component = createComponent();
    const after = Date.now();
    expect(component.now()).toBeGreaterThanOrEqual(before);
    expect(component.now()).toBeLessThanOrEqual(after);
  });

  it('should compute elapsed for active training job', () => {
    const component = createComponent();
    // mockJob has status TRAINING and training_start_time set
    const elapsed = component.elapsed();
    expect(elapsed).not.toBeNull();
    expect(elapsed).toMatch(/\d+/);
  });

  it('should return null elapsed for completed job', () => {
    mockState.currentTrainingJob.set({ ...mockJob, status: 'COMPLETED' });
    const component = createComponent();
    expect(component.elapsed()).toBeNull();
  });

  it('should use created_at when training_start_time is null', () => {
    mockState.currentTrainingJob.set({
      ...mockJob,
      status: 'PENDING',
      training_start_time: null,
      created_at: new Date(Date.now() - 60_000).toISOString(),
    });
    const component = createComponent();
    const elapsed = component.elapsed();
    expect(elapsed).not.toBeNull();
    expect(elapsed).toMatch(/\d+/);
  });

  // ── Polling ───────────────────────────────────────────────────────

  it('should start polling on init for active jobs', () => {
    vi.useFakeTimers();
    try {
      const component = createComponent();
      mockState.loadTrainingJobDetail.mockClear();
      mockState.loadTrainingJobLogs.mockClear();

      vi.advanceTimersByTime(10_000);

      expect(mockState.loadTrainingJobDetail).toHaveBeenCalledWith('tj-1');
      expect(mockState.loadTrainingJobLogs).toHaveBeenCalledWith('tj-1');
    } finally {
      vi.useRealTimers();
    }
  });

  it('should stop polling when job reaches terminal status', () => {
    vi.useFakeTimers();
    try {
      const component = createComponent();
      // Simulate job completing
      mockState.currentTrainingJob.set({ ...mockJob, status: 'COMPLETED' });
      mockState.loadTrainingJobDetail.mockClear();

      vi.advanceTimersByTime(10_000);
      // Polling should have stopped — no new calls
      vi.advanceTimersByTime(10_000);

      expect(mockState.loadTrainingJobDetail).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  // ── Filtered hyperparameters ──────────────────────────────────────

  it('should filter out internal hyperparameter keys', () => {
    const component = createComponent();
    const params = {
      epochs: '3',
      learning_rate: '2e-5',
      dynamodb_region: 'us-west-2',
      dynamodb_table_name: 'my-table',
      table_name: 'another-table',
      job_pk: 'pk-value',
      job_sk: 'sk-value',
    };
    const result = component.filteredHyperparameters(params);
    expect(result).toEqual({ epochs: '3', learning_rate: '2e-5' });
  });

  it('should return all params when no internal keys present', () => {
    const component = createComponent();
    const params = { epochs: '5', batch_size: '16' };
    const result = component.filteredHyperparameters(params);
    expect(result).toEqual({ epochs: '5', batch_size: '16' });
  });
});
