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
import { parseIso } from '../../utils/date';

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
    if (!a?.monthly_quota_usd || !a.current_month_usage_usd) return 0;
    return Math.min(100, (a.current_month_usage_usd / a.monthly_quota_usd) * 100);
  });

  /** Total number of training jobs. */
  readonly trainingJobCount = computed(() => this.trainingJobs().length);

  /** Total number of inference jobs. */
  readonly inferenceJobCount = computed(() => this.inferenceJobs().length);

  /** Training jobs currently in progress (PENDING or TRAINING). */
  readonly activeTrainingJobs = computed(() =>
    this.trainingJobs().filter(j => j.status === 'PENDING' || j.status === 'TRAINING'),
  );

  /** Inference jobs currently in progress (PENDING or TRANSFORMING). */
  readonly activeInferenceJobs = computed(() =>
    this.inferenceJobs().filter(j => j.status === 'PENDING' || j.status === 'TRANSFORMING'),
  );

  /** Training jobs that are no longer active. */
  readonly completedTrainingJobs = computed(() =>
    this.trainingJobs().filter(j => j.status !== 'PENDING' && j.status !== 'TRAINING'),
  );

  /** Inference jobs that are no longer active. */
  readonly completedInferenceJobs = computed(() =>
    this.inferenceJobs().filter(j => j.status !== 'PENDING' && j.status !== 'TRANSFORMING'),
  );

  /** Whether any job is currently in progress. */
  readonly hasActiveJobs = computed(() =>
    this.activeTrainingJobs().length > 0 || this.activeInferenceJobs().length > 0,
  );

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
   *
   * The list endpoints return cached DynamoDB data without syncing SageMaker.
   * To get fresh status for active jobs, we follow up with individual get-by-id
   * calls which trigger a SageMaker status sync on the backend.
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

        let trainingJobs = this.sortByCreatedDesc(trainingResponse.jobs);
        let inferenceJobs = this.sortByCreatedDesc(inferenceResponse.jobs);

        // Sync active jobs via get-by-id endpoints (triggers SageMaker status sync)
        const activeTraining = trainingJobs.filter(
          j => j.status === 'PENDING' || j.status === 'TRAINING',
        );
        const activeInference = inferenceJobs.filter(
          j => j.status === 'PENDING' || j.status === 'TRANSFORMING',
        );

        if (activeTraining.length > 0 || activeInference.length > 0) {
          const [syncedTraining, syncedInference] = await Promise.all([
            Promise.all(
              activeTraining.map(j =>
                firstValueFrom(this.http.getTrainingJob(j.job_id)).catch(() => null),
              ),
            ),
            Promise.all(
              activeInference.map(j =>
                firstValueFrom(this.http.getInferenceJob(j.job_id)).catch(() => null),
              ),
            ),
          ]);

          // Merge synced data back into the lists
          for (const synced of syncedTraining) {
            if (!synced) continue;
            const idx = trainingJobs.findIndex(j => j.job_id === synced.job_id);
            if (idx !== -1) trainingJobs = [...trainingJobs.slice(0, idx), synced, ...trainingJobs.slice(idx + 1)];
          }
          for (const synced of syncedInference) {
            if (!synced) continue;
            const idx = inferenceJobs.findIndex(j => j.job_id === synced.job_id);
            if (idx !== -1) inferenceJobs = [...inferenceJobs.slice(0, idx), synced, ...inferenceJobs.slice(idx + 1)];
          }
        }

        this.trainingJobs.set(trainingJobs);
        this.inferenceJobs.set(inferenceJobs);
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
    // Clear stale data from a previously viewed job to prevent flickering
    if (this.currentTrainingJob()?.job_id !== jobId) {
      this.currentTrainingJob.set(null);
      this.currentLogs.set([]);
    }
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
    // Clear stale data from a previously viewed job to prevent flickering
    if (this.currentInferenceJob()?.job_id !== jobId) {
      this.currentInferenceJob.set(null);
      this.currentLogs.set([]);
    }
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

  /** Sort jobs by created_at descending (newest first). */
  private sortByCreatedDesc<T extends { created_at: string }>(jobs: T[]): T[] {
    return [...jobs].sort((a, b) => parseIso(b.created_at).getTime() - parseIso(a.created_at).getTime());
  }
}
