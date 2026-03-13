/**
 * TypeScript interfaces for the user-facing fine-tuning feature.
 * Field names use snake_case to match backend FastAPI JSON responses.
 */

// ── Access ──────────────────────────────────────────────────────────────

export interface FineTuningAccessResponse {
  has_access: boolean;
  monthly_quota_hours: number | null;
  current_month_usage_hours: number | null;
  quota_period: string | null;
}

// ── Model Catalog ───────────────────────────────────────────────────────

export interface AvailableModel {
  model_id: string;
  model_name: string;
  huggingface_model_id: string;
  description: string;
  default_instance_type: string;
  default_hyperparameters: Record<string, string>;
}

// ── Presigned URL ───────────────────────────────────────────────────────

export interface PresignRequest {
  filename: string;
  content_type: string;
}

export interface PresignResponse {
  presigned_url: string;
  s3_key: string;
  expires_at: string;
}

// ── Training Job ────────────────────────────────────────────────────────

export type TrainingJobStatus = 'PENDING' | 'TRAINING' | 'COMPLETED' | 'FAILED' | 'STOPPED';

export interface CreateJobRequest {
  model_id: string;
  dataset_s3_key: string;
  instance_type?: string;
  hyperparameters?: Record<string, string>;
  max_runtime_seconds?: number;
}

export interface JobResponse {
  job_id: string;
  user_id: string;
  email: string;
  model_id: string;
  model_name: string;
  status: TrainingJobStatus;
  dataset_s3_key: string;
  output_s3_prefix: string | null;
  instance_type: string;
  instance_count: number;
  hyperparameters: Record<string, string> | null;
  sagemaker_job_name: string | null;
  training_start_time: string | null;
  training_end_time: string | null;
  billable_seconds: number | null;
  estimated_cost_usd: number | null;
  created_at: string;
  updated_at: string;
  error_message: string | null;
  max_runtime_seconds: number;
  training_progress: number | null;
}

export interface JobListResponse {
  jobs: JobResponse[];
  total_count: number;
}

// ── Inference Job ───────────────────────────────────────────────────────

export type InferenceJobStatus = 'PENDING' | 'TRANSFORMING' | 'COMPLETED' | 'FAILED' | 'STOPPED';

export interface CreateInferenceJobRequest {
  training_job_id: string;
  input_s3_key: string;
  instance_type?: string;
  max_runtime_seconds?: number;
}

export interface InferenceJobResponse {
  job_id: string;
  user_id: string;
  email: string;
  job_type: string;
  training_job_id: string;
  model_name: string;
  model_s3_path: string;
  status: InferenceJobStatus;
  input_s3_key: string;
  output_s3_prefix: string | null;
  result_s3_key: string | null;
  instance_type: string;
  transform_job_name: string | null;
  transform_start_time: string | null;
  transform_end_time: string | null;
  billable_seconds: number | null;
  estimated_cost_usd: number | null;
  created_at: string;
  updated_at: string;
  error_message: string | null;
  max_runtime_seconds: number;
}

export interface InferenceJobListResponse {
  jobs: InferenceJobResponse[];
  total_count: number;
}

// ── Trained Model (for inference selection) ─────────────────────────────

export interface TrainedModelResponse {
  training_job_id: string;
  model_id: string;
  model_name: string;
  model_s3_path: string;
  instance_type: string;
  completed_at: string | null;
  estimated_cost_usd: number | null;
}

// ── Logs & Download (detail page responses) ─────────────────────────────

export interface LogsResponse {
  logs: string[];
}

export interface DownloadResponse {
  download_url: string;
  expires_at: string;
  result_s3_key?: string;
}

// ── UI-only upload state ────────────────────────────────────────────────

export interface FileUploadState {
  file: File;
  progress: number;
  status: 'idle' | 'uploading' | 'complete' | 'error';
  s3Key?: string;
  error?: string;
}
