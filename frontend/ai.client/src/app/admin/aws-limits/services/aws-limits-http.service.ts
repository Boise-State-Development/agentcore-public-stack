import { Injectable, computed, inject } from '@angular/core';
import { HttpClient, HttpContext, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { SUPPRESS_ERROR_TOAST } from '../../../auth/error.interceptor';
import { ConfigService } from '../../../services/config.service';
import {
  AwsLimit,
  AwsLimitsDashboard,
  IncreaseHistory,
  IncreaseRequestBody,
  IncreaseRequestRecord,
} from '../models';

/** `/admin/aws-limits` (GitHub issue #12). The page shows its own errors, so no toast. */
@Injectable({ providedIn: 'root' })
export class AwsLimitsHttpService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/admin/aws-limits`);
  private readonly options = { context: new HttpContext().set(SUPPRESS_ERROR_TOAST, true) };

  getDashboard(options: { refresh?: boolean; allBedrock?: boolean } = {}): Promise<AwsLimitsDashboard> {
    let params = new HttpParams();
    if (options.refresh) params = params.set('refresh', 'true');
    if (options.allBedrock) params = params.set('all_bedrock', 'true');
    return firstValueFrom(this.http.get<AwsLimitsDashboard>(this.baseUrl(), { ...this.options, params }));
  }

  getQuota(serviceCode: string, quotaCode: string): Promise<AwsLimit> {
    const path = `${encodeURIComponent(serviceCode)}/${encodeURIComponent(quotaCode)}`;
    return firstValueFrom(this.http.get<AwsLimit>(`${this.baseUrl()}/${path}`, this.options));
  }

  getHistory(refresh = false): Promise<IncreaseHistory> {
    const params = refresh ? new HttpParams().set('refresh', 'true') : new HttpParams();
    return firstValueFrom(this.http.get<IncreaseHistory>(`${this.baseUrl()}/requests`, { ...this.options, params }));
  }

  requestIncrease(body: IncreaseRequestBody): Promise<IncreaseRequestRecord> {
    return firstValueFrom(this.http.post<IncreaseRequestRecord>(`${this.baseUrl()}/requests`, body, this.options));
  }
}

/** One sentence an admin can act on, from whatever the API refused with. */
export function awsLimitsErrorMessage(error: unknown, fallback: string): string {
  if (!(error instanceof HttpErrorResponse)) return fallback;
  const detail = (error.error as { detail?: unknown } | null)?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { msg?: unknown };
    if (typeof first?.msg === 'string') return first.msg.replace(/^Value error, /, '');
  }
  if (error.status === 403) return 'You do not have permission to do that.';
  if (error.status === 404) return 'Not found. AWS Limits may be switched off on this deployment.';
  return fallback;
}
