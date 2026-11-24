import { Injectable, computed, inject } from '@angular/core';
import { HttpClient, HttpContext, HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { SUPPRESS_ERROR_TOAST } from '../../../auth/error.interceptor';
import { ConfigService } from '../../../services/config.service';
import { SkillScriptsDetail, SkillScriptsSwitchBody } from '../models/skill-scripts.models';

/** `/admin/skill-scripts` (skill-code-sandbox §8.10). The page shows its own errors, so no toast. */
@Injectable({ providedIn: 'root' })
export class SkillScriptsHttpService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/admin/skill-scripts`);
  private readonly options = { context: new HttpContext().set(SUPPRESS_ERROR_TOAST, true) };

  getDetail(skillId: string): Promise<SkillScriptsDetail> {
    return firstValueFrom(
      this.http.get<SkillScriptsDetail>(`${this.baseUrl()}/${encodeURIComponent(skillId)}`, this.options),
    );
  }

  setSwitch(skillId: string, body: SkillScriptsSwitchBody): Promise<SkillScriptsDetail> {
    return firstValueFrom(
      this.http.put<SkillScriptsDetail>(`${this.baseUrl()}/${encodeURIComponent(skillId)}/switch`, body, this.options),
    );
  }
}

/** One sentence an admin can act on, from whatever the API refused with. */
export function skillScriptsErrorMessage(error: unknown, fallback: string): string {
  if (!(error instanceof HttpErrorResponse)) return fallback;
  if (error.status === 404) return 'No skill has that id.';
  if (error.status === 403) return 'You do not have permission to do that.';
  const detail = (error.error as { detail?: unknown } | null)?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { msg?: unknown };
    if (typeof first?.msg === 'string') return first.msg.replace(/^Value error, /, '');
  }
  return fallback;
}
