import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { signal } from '@angular/core';
import { FineTuningDashboardPage } from './fine-tuning-dashboard.page';
import { FineTuningStateService } from '../../services/fine-tuning-state.service';
import type { JobResponse, InferenceJobResponse, FineTuningAccessResponse } from '../../models/fine-tuning.models';

const mockAccess: FineTuningAccessResponse = {
  has_access: true,
  monthly_quota_hours: 10,
  current_month_usage_hours: 3,
  quota_period: '2026-03',
};

const mockTrainingJob: JobResponse = {
  job_id: 'tj-1',
  user_id: 'u1',
  email: 'test@example.com',
  model_id: 'model-1',
  model_name: 'Test Model',
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
});
