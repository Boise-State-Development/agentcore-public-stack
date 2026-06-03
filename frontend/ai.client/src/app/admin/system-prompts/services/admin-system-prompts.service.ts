import { Injectable, inject, computed, resource, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import {
  SystemPromptAdmin,
  SystemPromptFormData,
  SystemPromptsAdminListResponse,
} from '../models/system-prompt-admin.model';

/**
 * Service for admin CRUD on the system prompts catalog.
 *
 * Lazy-loaded: the resource only fires after the admin manage page calls
 * `ensureLoaded()` to avoid 401s for non-admin users.
 */
@Injectable({ providedIn: 'root' })
export class AdminSystemPromptsService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);

  private readonly baseUrl = computed(
    () => `${this.config.appApiUrl()}/admin/system-prompts`,
  );

  private readonly loadRequested = signal(false);

  readonly promptsResource = resource({
    params: () => (this.loadRequested() ? {} : undefined),
    loader: async () => this.fetchAll(),
  });

  ensureLoaded(): void {
    this.loadRequested.set(true);
  }

  async fetchAll(): Promise<SystemPromptsAdminListResponse> {
    return firstValueFrom(
      this.http.get<SystemPromptsAdminListResponse>(`${this.baseUrl()}/`),
    );
  }

  async getPrompt(promptId: string): Promise<SystemPromptAdmin> {
    return firstValueFrom(
      this.http.get<SystemPromptAdmin>(`${this.baseUrl()}/${promptId}`),
    );
  }

  async createPrompt(data: SystemPromptFormData): Promise<SystemPromptAdmin> {
    const created = await firstValueFrom(
      this.http.post<SystemPromptAdmin>(`${this.baseUrl()}/`, data),
    );
    this.promptsResource.reload();
    return created;
  }

  async updatePrompt(
    promptId: string,
    updates: Partial<SystemPromptFormData>,
  ): Promise<SystemPromptAdmin> {
    const updated = await firstValueFrom(
      this.http.patch<SystemPromptAdmin>(`${this.baseUrl()}/${promptId}`, updates),
    );
    this.promptsResource.reload();
    return updated;
  }

  async deletePrompt(promptId: string): Promise<void> {
    await firstValueFrom(
      this.http.delete<void>(`${this.baseUrl()}/${promptId}`),
    );
    this.promptsResource.reload();
  }
}
