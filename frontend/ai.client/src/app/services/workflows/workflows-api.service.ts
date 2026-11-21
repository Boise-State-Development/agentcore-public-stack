import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { ConfigService } from '../config.service';
import { WorkflowDefinition, WorkflowDocument, WorkflowListResponse, WorkflowRef, WorkflowSummary, WorkflowView } from './workflow.models';

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
      reads: [...step.reads],
      request: step.request ?? null,
    })),
    result: {
      mode: view.result.mode,
      reads: view.result.reads ? [...view.result.reads] : null,
      instruction: view.result.instruction ?? null,
    },
  };
}
