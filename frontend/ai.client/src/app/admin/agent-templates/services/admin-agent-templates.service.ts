import { Injectable, inject, computed, resource, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import {
  AgentTemplateAdmin,
  AgentTemplateAdminListResponse,
  AgentTemplateCreate,
  AgentTemplateUpdate,
} from '../models/agent-template-admin.model';

/**
 * Admin CRUD on the agent templates catalog — the write side of the
 * `/admin/agent-templates` surface. The public picker reads the enabled subset
 * from `GET /templates` instead.
 *
 * Lazy-loaded like `AdminSystemPromptsService`: the resource only fires after
 * the manage page calls `ensureLoaded()`, so a non-admin page that happens to
 * inject the service never triggers a guaranteed 401.
 */
@Injectable({ providedIn: 'root' })
export class AdminAgentTemplatesService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);

  private readonly baseUrl = computed(
    () => `${this.config.appApiUrl()}/admin/agent-templates`,
  );

  private readonly loadRequested = signal(false);

  readonly templatesResource = resource({
    params: () => (this.loadRequested() ? {} : undefined),
    loader: async () => this.fetchAll(),
  });

  ensureLoaded(): void {
    this.loadRequested.set(true);
  }

  async fetchAll(): Promise<AgentTemplateAdminListResponse> {
    return firstValueFrom(
      this.http.get<AgentTemplateAdminListResponse>(`${this.baseUrl()}/`),
    );
  }

  async getTemplate(templateId: string): Promise<AgentTemplateAdmin> {
    return firstValueFrom(
      this.http.get<AgentTemplateAdmin>(`${this.baseUrl()}/${templateId}`),
    );
  }

  async createTemplate(data: AgentTemplateCreate): Promise<AgentTemplateAdmin> {
    const created = await firstValueFrom(
      this.http.post<AgentTemplateAdmin>(`${this.baseUrl()}/`, data),
    );
    this.templatesResource.reload();
    return created;
  }

  async updateTemplate(
    templateId: string,
    updates: AgentTemplateUpdate,
  ): Promise<AgentTemplateAdmin> {
    const updated = await firstValueFrom(
      this.http.patch<AgentTemplateAdmin>(`${this.baseUrl()}/${templateId}`, updates),
    );
    this.templatesResource.reload();
    return updated;
  }

  async deleteTemplate(templateId: string): Promise<void> {
    await firstValueFrom(
      this.http.delete<void>(`${this.baseUrl()}/${templateId}`),
    );
    this.templatesResource.reload();
  }
}
