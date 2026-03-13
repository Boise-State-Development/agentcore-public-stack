import { Injectable, inject, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { ConfigService } from '../../services/config.service';
import {
  FineTuningAccessResponse,
  AvailableModel,
  PresignRequest,
  PresignResponse,
  CreateJobRequest,
  JobResponse,
  JobListResponse,
  CreateInferenceJobRequest,
  InferenceJobResponse,
  InferenceJobListResponse,
  TrainedModelResponse,
  LogsResponse,
  DownloadResponse,
} from '../models/fine-tuning.models';

/**
 * HTTP service for user-facing fine-tuning API endpoints.
 * Communicates with FastAPI backend /fine-tuning endpoints.
 */
@Injectable({
  providedIn: 'root',
})
export class FineTuningHttpService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private baseUrl = computed(() => `${this.config.appApiUrl()}/fine-tuning`);

  // ── Access ──────────────────────────────────────────────────────────

  /** Check if the current user has fine-tuning access and get quota info. */
  checkAccess(): Observable<FineTuningAccessResponse> {
    return this.http.get<FineTuningAccessResponse>(`${this.baseUrl()}/access`);
  }

  // ── Model Catalog ───────────────────────────────────────────────────

  /** List available base models for fine-tuning. */
  listModels(): Observable<AvailableModel[]> {
    return this.http.get<AvailableModel[]>(`${this.baseUrl()}/models`);
  }

  // ── Training Jobs ───────────────────────────────────────────────────

  /** Get a presigned S3 URL for uploading a training dataset. */
  presignDatasetUpload(request: PresignRequest): Observable<PresignResponse> {
    return this.http.post<PresignResponse>(`${this.baseUrl()}/presign`, request);
  }

  /** Create a new training job. */
  createTrainingJob(request: CreateJobRequest): Observable<JobResponse> {
    return this.http.post<JobResponse>(`${this.baseUrl()}/jobs`, request);
  }

  /** List all training jobs for the current user. */
  listTrainingJobs(): Observable<JobListResponse> {
    return this.http.get<JobListResponse>(`${this.baseUrl()}/jobs`);
  }

  /** Get a single training job by ID (syncs SageMaker status). */
  getTrainingJob(jobId: string): Observable<JobResponse> {
    return this.http.get<JobResponse>(`${this.baseUrl()}/jobs/${encodeURIComponent(jobId)}`);
  }

  /** Stop a running training job. */
  stopTrainingJob(jobId: string): Observable<JobResponse> {
    return this.http.delete<JobResponse>(`${this.baseUrl()}/jobs/${encodeURIComponent(jobId)}`);
  }

  /** Get CloudWatch logs for a training job. */
  getTrainingJobLogs(jobId: string): Observable<LogsResponse> {
    return this.http.get<LogsResponse>(`${this.baseUrl()}/jobs/${encodeURIComponent(jobId)}/logs`);
  }

  /** Get a presigned download URL for a completed training job's model artifact. */
  downloadTrainingArtifact(jobId: string): Observable<DownloadResponse> {
    return this.http.get<DownloadResponse>(`${this.baseUrl()}/jobs/${encodeURIComponent(jobId)}/download`);
  }

  // ── Trained Models ──────────────────────────────────────────────────

  /** List completed training jobs available for inference. */
  listTrainedModels(): Observable<TrainedModelResponse[]> {
    return this.http.get<TrainedModelResponse[]>(`${this.baseUrl()}/trained-models`);
  }

  // ── Inference Jobs ──────────────────────────────────────────────────

  /** Get a presigned S3 URL for uploading inference input data. */
  presignInferenceUpload(request: PresignRequest): Observable<PresignResponse> {
    return this.http.post<PresignResponse>(`${this.baseUrl()}/inference/presign`, request);
  }

  /** Create a new inference (batch transform) job. */
  createInferenceJob(request: CreateInferenceJobRequest): Observable<InferenceJobResponse> {
    return this.http.post<InferenceJobResponse>(`${this.baseUrl()}/inference`, request);
  }

  /** List all inference jobs for the current user. */
  listInferenceJobs(): Observable<InferenceJobListResponse> {
    return this.http.get<InferenceJobListResponse>(`${this.baseUrl()}/inference`);
  }

  /** Get a single inference job by ID (syncs SageMaker status). */
  getInferenceJob(jobId: string): Observable<InferenceJobResponse> {
    return this.http.get<InferenceJobResponse>(
      `${this.baseUrl()}/inference/${encodeURIComponent(jobId)}`,
    );
  }

  /** Stop a running inference job. */
  stopInferenceJob(jobId: string): Observable<InferenceJobResponse> {
    return this.http.delete<InferenceJobResponse>(
      `${this.baseUrl()}/inference/${encodeURIComponent(jobId)}`,
    );
  }

  /** Get CloudWatch logs for an inference job. */
  getInferenceJobLogs(jobId: string): Observable<LogsResponse> {
    return this.http.get<LogsResponse>(
      `${this.baseUrl()}/inference/${encodeURIComponent(jobId)}/logs`,
    );
  }

  /** Get a presigned download URL for completed inference results. */
  downloadInferenceResults(jobId: string): Observable<DownloadResponse> {
    return this.http.get<DownloadResponse>(
      `${this.baseUrl()}/inference/${encodeURIComponent(jobId)}/download`,
    );
  }
}
