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
