import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, computed, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { ConfigService } from '../services/config.service';
import {
  WorkflowDocument,
  WorkflowListResponse,
  WorkflowRef,
  WorkflowWrite,
} from './workflow.models';

/** Opening one workflow. A stored file that no longer parses is an answer, not an exception. */
export type WorkflowReadResult =
  | { ok: true; document: WorkflowDocument }
  | { ok: false; message: string };

/**
 * Saving one workflow. `conflict` is the 409 a stale `expectedVersion` earns: another tab saved
 * first (`document` is what is stored now) or deleted it (`document` is null).
 */
export type WorkflowSaveResult =
  | { ok: true; document: WorkflowDocument }
  | { ok: false; conflict: true; message: string; document: WorkflowDocument | null }
  | { ok: false; conflict: false; message: string };

/**
 * The `/workflows` routes (docs/specs/workflow-designer.md §9.2). Stateless: the list page and the
 * designer each hold what they show, so there is no shared cache to go stale between two tabs.
 */
@Injectable({ providedIn: 'root' })
export class WorkflowService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/workflows`);

  private url(ref: WorkflowRef): string {
    return `${this.baseUrl()}/${encodeURIComponent(ref.skillId)}/${encodeURIComponent(ref.name)}`;
  }

  list(): Promise<WorkflowListResponse> {
    return firstValueFrom(this.http.get<WorkflowListResponse>(this.baseUrl(), { withCredentials: true }));
  }

  /** A new carrying skill and its file: blank, or filled from a started run. */
  create(body: { title?: string; fromRun?: { sessionId: string; runId: string } }): Promise<WorkflowDocument> {
    return firstValueFrom(this.http.post<WorkflowDocument>(this.baseUrl(), body, { withCredentials: true }));
  }

  async read(ref: WorkflowRef): Promise<WorkflowReadResult> {
    try {
      const document = await firstValueFrom(this.http.get<WorkflowDocument>(this.url(ref), { withCredentials: true }));
      return { ok: true, document };
    } catch (err) {
      if (err instanceof HttpErrorResponse && err.status === 400) {
        return { ok: false, message: this.messageFor(err, 'This workflow’s file could not be read.') };
      }
      throw err;
    }
  }

  /** The sheet a starter would see for this draft (D3). Writes nothing and makes no model call. */
  validate(ref: WorkflowRef, body: WorkflowWrite): Promise<WorkflowDocument> {
    const draft: WorkflowWrite = { ...body };
    delete draft.expectedVersion;
    return firstValueFrom(
      this.http.put<WorkflowDocument>(this.url(ref), draft, { withCredentials: true, params: { validate: 'true' } }),
    );
  }

  async save(ref: WorkflowRef, body: WorkflowWrite): Promise<WorkflowSaveResult> {
    try {
      const document = await firstValueFrom(this.http.put<WorkflowDocument>(this.url(ref), body, { withCredentials: true }));
      return { ok: true, document };
    } catch (err) {
      if (err instanceof HttpErrorResponse && err.status === 409) {
        const body = err.error as { code?: unknown; document?: WorkflowDocument | null } | null;
        if (body?.code === 'version_conflict' || body?.code === 'workflow_deleted') {
          return {
            ok: false,
            conflict: true,
            message:
              body.code === 'workflow_deleted'
                ? 'This workflow was deleted somewhere else.'
                : 'This workflow changed somewhere else since you opened it.',
            document: body.document ?? null,
          };
        }
      }
      return { ok: false, conflict: false, message: this.messageFor(err, 'The workflow couldn’t be saved.') };
    }
  }

  async remove(ref: WorkflowRef): Promise<void> {
    await firstValueFrom(this.http.delete(this.url(ref), { withCredentials: true }));
  }

  /** The backend's own message (`detail`), so a parser refusal reads as written. */
  messageFor(err: unknown, fallback: string): string {
    const detail = (err as { error?: { detail?: unknown } } | null)?.error?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: unknown };
      if (typeof first?.msg === 'string') return first.msg;
    }
    return fallback;
  }
}
