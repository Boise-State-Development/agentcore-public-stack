/** Shape of a fine-tuning access grant returned by the admin API. */
export interface FineTuningGrant {
  email: string;
  granted_by: string;
  granted_at: string;
  monthly_quota_hours: number;
  current_month_usage_hours: number;
  quota_period: string;
}

/** Response wrapper for the list-all-grants endpoint. */
export interface AccessListResponse {
  grants: FineTuningGrant[];
  total_count: number;
}

/** Per-user cost breakdown for a billing period. */
export interface UserCostBreakdown {
  email: string;
  total_cost_usd: number;
  total_gpu_hours: number;
  training_job_count: number;
  inference_job_count: number;
}

/** Aggregated cost dashboard response. */
export interface FineTuningCostDashboard {
  period: string;
  total_cost_usd: number;
  total_gpu_hours: number;
  active_user_count: number;
  training_job_count: number;
  inference_job_count: number;
  users: UserCostBreakdown[];
}
