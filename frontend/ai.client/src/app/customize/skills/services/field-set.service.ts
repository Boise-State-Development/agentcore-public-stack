import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, computed, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import {
  FieldSet,
  FieldSetListResponse,
  FieldSetReadError,
  FieldSetWrite,
  fieldSetFilename,
} from '../models/field-set.model';

/**
 * Outcome of opening one set. A stored file that no longer parses is not an
 * exception here: it is a normal thing to open, in markdown mode, to fix.
 */
export type FieldSetReadResult = { ok: true; set: FieldSet } | { ok: false; error: FieldSetReadError };

/**
 * The field-set routes on a skill the user owns
 * (`/skills/mine/{skillId}/field-sets`, field-set-authoring §4.1). Stateless:
 * the editor page holds what it is editing, and the skill form's list section
 * holds its list, so there is no shared cache to go stale between them.
 */
@Injectable({ providedIn: 'root' })
export class FieldSetService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/skills/mine`);

  private setsUrl(skillId: string): string {
    return `${this.baseUrl()}/${encodeURIComponent(skillId)}/field-sets`;
  }

  list(skillId: string): Promise<FieldSetListResponse> {
    return firstValueFrom(
      this.http.get<FieldSetListResponse>(this.setsUrl(skillId), { withCredentials: true }),
    );
  }

  async read(skillId: string, name: string): Promise<FieldSetReadResult> {
    try {
      const set = await firstValueFrom(
        this.http.get<FieldSet>(`${this.setsUrl(skillId)}/${encodeURIComponent(name)}`, {
          withCredentials: true,
        }),
      );
      return { ok: true, set };
    } catch (err) {
      if (err instanceof HttpErrorResponse && err.status === 400) {
        const body = err.error as { detail?: unknown; markdown?: unknown } | null;
        return {
          ok: false,
          error: {
            message: typeof body?.detail === 'string' ? body.detail : 'This field set could not be read.',
            markdown: typeof body?.markdown === 'string' ? body.markdown : null,
          },
        };
      }
      throw err;
    }
  }

  /** Save one set. `validate` returns what would be stored and stores nothing. */
  write(skillId: string, name: string, body: FieldSetWrite, validate = false): Promise<FieldSet> {
    return firstValueFrom(
      this.http.put<FieldSet>(`${this.setsUrl(skillId)}/${encodeURIComponent(name)}`, body, {
        withCredentials: true,
        params: validate ? { validate: 'true' } : {},
      }),
    );
  }

  /** Delete one set: the ordinary bundle-file delete, not a route of its own. */
  async remove(skillId: string, name: string): Promise<void> {
    await firstValueFrom(
      this.http.delete(
        `${this.baseUrl()}/${encodeURIComponent(skillId)}/resources/${encodeURIComponent(fieldSetFilename(name))}`,
        { withCredentials: true },
      ),
    );
  }

  /** The backend's own message (`detail`), so a parser refusal reads as written. */
  messageFor(err: unknown, fallback: string): string {
    const detail = (err as { error?: { detail?: unknown } } | null)?.error?.detail;
    if (typeof detail === 'string' && detail.trim()) {
      return detail;
    }
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: unknown };
      if (typeof first?.msg === 'string') {
        return first.msg;
      }
    }
    return fallback;
  }
}
