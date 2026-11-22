import { HttpClient, HttpContext, HttpErrorResponse } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { SUPPRESS_ERROR_TOAST } from '../../auth/error.interceptor';
import { ConfigService } from '../config.service';
import { WorkflowDefinition, WorkflowDocument, WorkflowListResponse, WorkflowRef, WorkflowSummary, WorkflowView } from './workflow.models';

/** A `PUT` body made against a stored version: a save that finds another version stored is a 409. */
export type WorkflowWrite = WorkflowDefinition & { expectedVersion?: number };

/** Opening one workflow in the designer. A stored file that no longer parses is an answer, not an exception. */
export type WorkflowReadResult = { ok: true; document: WorkflowDocument } | { ok: false; message: string };

/**
 * The designer's save. `conflict` is the 409 a stale `expectedVersion` earns: another tab saved first
 * (`document` is what is stored now) or deleted it (`document` is null).
 */
export type WorkflowSaveResult =
  | { ok: true; document: WorkflowDocument }
  | { ok: false; conflict: true; message: string; document: WorkflowDocument | null }
  | { ok: false; conflict: false; message: string };

/**
 * The designer, the Workflows page and Save as workflow each show their own inline error, so their
 * calls raise no global toast: a 409 from another tab's save is an answer, not a failure to report
 * twice.
 */
function quiet(): HttpContext {
  return new HttpContext().set(SUPPRESS_ERROR_TOAST, true);
}

/**
 * `/workflows` (docs/specs/workflow-designer.md §9.2). Reads and writes only; who may call it is
 * the caller's to gate (`features.workflows`), and the backend 404s while the flag is off.
 */
@Injectable({ providedIn: 'root' })
export class WorkflowsApiService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);

  /** Every workflow the caller owns or can use: theirs first, then shared, each by title. */
  async list(): Promise<WorkflowSummary[]> {
    const response = await firstValueFrom(this.http.get<WorkflowListResponse>(this.baseUrl()));
    return sortOwnedFirst(response.workflows ?? []);
  }

  get(ref: WorkflowRef): Promise<WorkflowDocument> {
    return firstValueFrom(this.http.get<WorkflowDocument>(this.url(ref)));
  }

  /** Write a workflow the caller owns. A save that changes nothing writes nothing (`written: false`). */
  save(ref: WorkflowRef, definition: WorkflowDefinition): Promise<WorkflowDocument> {
    return firstValueFrom(this.http.put<WorkflowDocument>(this.url(ref), definition));
  }

  // ---- The designer's calls (PR-D.3). Each caller renders its own error, so none raises a toast. ----

  /** Every workflow, for the Workflows page: the caller's first. */
  async listQuietly(): Promise<WorkflowSummary[]> {
    const response = await firstValueFrom(this.http.get<WorkflowListResponse>(this.baseUrl(), { context: quiet() }));
    return sortOwnedFirst(response.workflows ?? []);
  }

  /** A new carrying skill and its file: blank, or filled from a started run (`fromRun`). */
  create(body: { title?: string; fromRun?: { sessionId: string; runId: string } }): Promise<WorkflowDocument> {
    return firstValueFrom(this.http.post<WorkflowDocument>(this.baseUrl(), body, { context: quiet() }));
  }

  async read(ref: WorkflowRef): Promise<WorkflowReadResult> {
    try {
      return { ok: true, document: await firstValueFrom(this.http.get<WorkflowDocument>(this.url(ref), { context: quiet() })) };
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
      this.http.put<WorkflowDocument>(this.url(ref), draft, { context: quiet(), params: { validate: 'true' } }),
    );
  }

  /** Save the designer's draft against the version it opened; a stale one comes back as a conflict. */
  async write(ref: WorkflowRef, body: WorkflowWrite): Promise<WorkflowSaveResult> {
    try {
      return { ok: true, document: await firstValueFrom(this.http.put<WorkflowDocument>(this.url(ref), body, { context: quiet() })) };
    } catch (err) {
      if (err instanceof HttpErrorResponse && err.status === 409) {
        const answer = err.error as { code?: unknown; document?: WorkflowDocument | null } | null;
        if (answer?.code === 'version_conflict' || answer?.code === 'workflow_deleted') {
          return {
            ok: false,
            conflict: true,
            message:
              answer.code === 'workflow_deleted'
                ? 'This workflow was deleted somewhere else.'
                : 'This workflow changed somewhere else since you opened it.',
            document: answer.document ?? null,
          };
        }
      }
      return { ok: false, conflict: false, message: this.messageFor(err, 'The workflow couldn’t be saved.') };
    }
  }

  async remove(ref: WorkflowRef): Promise<void> {
    await firstValueFrom(this.http.delete(this.url(ref), { context: quiet() }));
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

  private url(ref: WorkflowRef): string {
    return `${this.baseUrl()}/${encodeURIComponent(ref.skillId)}/${encodeURIComponent(ref.name)}`;
  }

  private baseUrl(): string {
    return `${this.config.appApiUrl()}/workflows`;
  }
}

/** The caller's own workflows first, then the ones shared with them; the server's order within each. */
export function sortOwnedFirst(workflows: readonly WorkflowSummary[]): WorkflowSummary[] {
  return [...workflows.filter((w) => w.isOwned), ...workflows.filter((w) => !w.isOwned)];
}

/** A workflow as `PUT` takes it back, from the document `GET` gave. */
export function definitionOf(view: WorkflowView): WorkflowDefinition {
  return {
    title: view.title,
    files: view.files,
    request: view.request ?? null,
    steps: view.steps.map((step) => ({
      name: step.name,
      kind: step.kind,
      agentId: step.agentId,
      agentVersion: step.agentVersion ?? null,
      // A prompt or checklist step's model goes back as the file names it, never its retirement successor.
      ...(step.kind === 'prompt' || step.kind === 'checklist'
        ? { modelId: step.modelId ?? step.model?.requestedModelId ?? step.model?.modelId ?? null }
        : {}),
      reads: [...step.reads],
      request: step.request ?? null,
      // A checklist step's sets, in the order it runs them: dropping them would make the step one the format refuses.
      ...(step.kind === 'checklist' ? { fieldSets: (step.fieldSets ?? []).map((set) => set.name) } : {}),
    })),
    result: {
      mode: view.result.mode,
      reads: view.result.reads ? [...view.result.reads] : null,
      instruction: view.result.instruction ?? null,
    },
  };
}
