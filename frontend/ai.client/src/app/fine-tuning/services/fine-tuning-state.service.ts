import { Injectable, inject, signal, computed } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { FineTuningHttpService } from './fine-tuning-http.service';
import {
  FineTuningAccessResponse,
  AvailableModel,
  JobResponse,
  InferenceJobResponse,
  TrainedModelResponse,
  CreateJobRequest,
  CreateInferenceJobRequest,
  DownloadResponse,
} from '../models/fine-tuning.models';

/**
 * State service for the user-facing fine-tuning feature.
 * Manages access info, job lists, model catalog, and CRUD operations.
 */
@Injectable({
  providedIn: 'root',
})
export class FineTuningStateService {
  private http = inject(FineTuningHttpService);

  // ── State signals ───────────────────────────────────────────────────

  /** Current user's fine-tuning access and quota info. */
  readonly access = signal<FineTuningAccessResponse | null>(null);

  /** User's training jobs (newest first). */
  readonly trainingJobs = signal<JobResponse[]>([]);

  /** User's inference jobs (newest first). */
  readonly inferenceJobs = signal<InferenceJobResponse[]>([]);

  /** Available base models for fine-tuning. */
  readonly availableModels = signal<AvailableModel[]>([]);

  /** Completed training jobs available for inference. */
  readonly trainedModels = signal<TrainedModelResponse[]>([]);

  /** Current training job for detail page. */
  readonly currentTrainingJob = signal<JobResponse | null>(null);

  /** Current inference job for detail page. */
  readonly currentInferenceJob = signal<InferenceJobResponse | null>(null);

  /** Logs for the current detail page job. */
  readonly currentLogs = signal<string[]>([]);

  /** Whether a network request is in progress. */
  readonly loading = signal(false);

  /** Last error message (null when clear). */
  readonly error = signal<string | null>(null);

  // ── Computed signals ────────────────────────────────────────────────

  /** Whether the user has fine-tuning access. */
  readonly hasAccess = computed(() => this.access()?.has_access ?? false);

  /** Quota usage percentage (0-100). */
  readonly quotaUsedPercent = computed(() => {
    const a = this.access();
    if (!a?.monthly_quota_hours || !a.current_month_usage_hours) return 0;
    return Math.min(100, (a.current_month_usage_hours / a.monthly_quota_hours) * 100);
  });

  /** Total number of training jobs. */
  readonly trainingJobCount = computed(() => this.trainingJobs().length);

  /** Total number of inference jobs. */
  readonly inferenceJobCount = computed(() => this.inferenceJobs().length);

  // ── Actions ─────────────────────────────────────────────────────────

  /** Check the current user's fine-tuning access and quota. */
  async checkAccess(): Promise<void> {
    try {
      const response = await firstValueFrom(this.http.checkAccess());
      this.access.set(response);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to check fine-tuning access';
      this.error.set(message);
    }
  }

  /**
   * Load dashboard data: access check, then training + inference jobs.
   * Only loads jobs if the user has access.
   */
  async loadDashboard(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      await this.checkAccess();
      if (this.hasAccess()) {
        const [trainingResponse, inferenceResponse] = await Promise.all([
          firstValueFrom(this.http.listTrainingJobs()),
          firstValueFrom(this.http.listInferenceJobs()),
        ]);
        this.trainingJobs.set(trainingResponse.jobs);
        this.inferenceJobs.set(inferenceResponse.jobs);
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to load dashboard data';
      this.error.set(message);
    } finally {
      this.loading.set(false);
    }
  }

  /** Load the available base model catalog. */
  async loadAvailableModels(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const models = await firstValueFrom(this.http.listModels());
      this.availableModels.set(models);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to load available models';
      this.error.set(message);
    } finally {
      this.loading.set(false);
    }
  }

  /** Load completed training jobs for inference model selection. */
  async loadTrainedModels(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const models = await firstValueFrom(this.http.listTrainedModels());
      this.trainedModels.set(models);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to load trained models';
      this.error.set(message);
    } finally {
      this.loading.set(false);
    }
  }

  /** Create a training job and refresh the dashboard. */
  async createTrainingJob(request: CreateJobRequest): Promise<JobResponse> {
    this.error.set(null);
    try {
      const job = await firstValueFrom(this.http.createTrainingJob(request));
      await this.loadDashboard();
      return job;
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to create training job';
      this.error.set(message);
      throw err;
    }
  }

  /** Create an inference job and refresh the dashboard. */
  async createInferenceJob(request: CreateInferenceJobRequest): Promise<InferenceJobResponse> {
    this.error.set(null);
    try {
      const job = await firstValueFrom(this.http.createInferenceJob(request));
      await this.loadDashboard();
      return job;
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to create inference job';
      this.error.set(message);
      throw err;
    }
  }

  /** Stop a running training job and refresh the dashboard. */
  async stopTrainingJob(jobId: string): Promise<void> {
    this.error.set(null);
    try {
      await firstValueFrom(this.http.stopTrainingJob(jobId));
      await this.loadDashboard();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to stop training job';
      this.error.set(message);
    }
  }

  /** Stop a running inference job and refresh the dashboard. */
  async stopInferenceJob(jobId: string): Promise<void> {
    this.error.set(null);
    try {
      await firstValueFrom(this.http.stopInferenceJob(jobId));
      await this.loadDashboard();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to stop inference job';
      this.error.set(message);
    }
  }

  // ── Detail page actions ────────────────────────────────────────────

  /** Load a single training job's detail (syncs SageMaker status). */
  async loadTrainingJobDetail(jobId: string): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const job = await firstValueFrom(this.http.getTrainingJob(jobId));
      this.currentTrainingJob.set(job);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to load training job';
      this.error.set(message);
    } finally {
      this.loading.set(false);
    }
  }

  /** Load CloudWatch logs for a training job. */
  async loadTrainingJobLogs(jobId: string): Promise<void> {
    try {
      const response = await firstValueFrom(this.http.getTrainingJobLogs(jobId));
      this.currentLogs.set(response.logs);
    } catch {
      // Logs may not be available yet — silently set empty
      this.currentLogs.set([]);
    }
  }

  /** Get a presigned download URL for a training job's model artifact. */
  async getTrainingDownloadUrl(jobId: string): Promise<DownloadResponse> {
    return firstValueFrom(this.http.downloadTrainingArtifact(jobId));
  }

  /** Load a single inference job's detail (syncs SageMaker status). */
  async loadInferenceJobDetail(jobId: string): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const job = await firstValueFrom(this.http.getInferenceJob(jobId));
      this.currentInferenceJob.set(job);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to load inference job';
      this.error.set(message);
    } finally {
      this.loading.set(false);
    }
  }

  /** Load CloudWatch logs for an inference job. */
  async loadInferenceJobLogs(jobId: string): Promise<void> {
    try {
      const response = await firstValueFrom(this.http.getInferenceJobLogs(jobId));
      this.currentLogs.set(response.logs);
    } catch {
      // Logs may not be available yet — silently set empty
      this.currentLogs.set([]);
    }
  }

  /** Get a presigned download URL for inference results. */
  async getInferenceDownloadUrl(jobId: string): Promise<DownloadResponse> {
    return firstValueFrom(this.http.downloadInferenceResults(jobId));
  }

  /** Clear the current error. */
  clearError(): void {
    this.error.set(null);
  }
}
