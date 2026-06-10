import { Injectable, inject, resource, signal, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import {
  AdminSkill,
  AdminSkillListResponse,
  SkillCreateRequest,
  SkillUpdateRequest,
  SkillRolesResponse,
  SkillRoleAssignment,
  SkillResourceRef,
  SkillResourcesResponse,
} from '../models/admin-skill.model';

/**
 * Service for admin skill management.
 *
 * Mirrors `AdminToolService`: `resource()`-backed catalog + signal state,
 * CRUD, role-grant sync, plus the reference-file (S3-backed) endpoints
 * introduced in PR-4.
 */
@Injectable({
  providedIn: 'root',
})
export class AdminSkillService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/admin/skills`);

  private _loading = signal(false);
  private _error = signal<string | null>(null);

  readonly loading = this._loading.asReadonly();
  readonly error = this._error.asReadonly();

  /**
   * Reactive resource for fetching admin skills.
   */
  readonly skillsResource = resource({
    loader: async () => {
      await Promise.resolve();
      return this.fetchSkills();
    },
  });

  /**
   * Get all skills from the cached resource.
   */
  getSkills(): AdminSkill[] {
    return this.skillsResource.value()?.skills ?? [];
  }

  /**
   * Get a skill by ID from the cached resource.
   */
  getSkillById(skillId: string): AdminSkill | undefined {
    return this.getSkills().find((s) => s.skillId === skillId);
  }

  /**
   * Fetch all skills from the API.
   */
  async fetchSkills(status?: string): Promise<AdminSkillListResponse> {
    let url = `${this.baseUrl()}/`;
    if (status) {
      url += `?status=${status}`;
    }
    return firstValueFrom(this.http.get<AdminSkillListResponse>(url));
  }

  /**
   * Fetch a single skill by ID.
   */
  async fetchSkill(skillId: string): Promise<AdminSkill> {
    return firstValueFrom(this.http.get<AdminSkill>(`${this.baseUrl()}/${skillId}`));
  }

  /**
   * Create a new skill.
   */
  async createSkill(skillData: SkillCreateRequest): Promise<AdminSkill> {
    this._loading.set(true);
    this._error.set(null);
    try {
      const response = await firstValueFrom(
        this.http.post<AdminSkill>(`${this.baseUrl()}/`, skillData)
      );
      this.skillsResource.reload();
      return response;
    } catch (err: unknown) {
      this._error.set(err instanceof Error ? err.message : 'Failed to create skill');
      throw err;
    } finally {
      this._loading.set(false);
    }
  }

  /**
   * Update an existing skill.
   */
  async updateSkill(skillId: string, updates: SkillUpdateRequest): Promise<AdminSkill> {
    this._loading.set(true);
    this._error.set(null);
    try {
      const response = await firstValueFrom(
        this.http.put<AdminSkill>(`${this.baseUrl()}/${skillId}`, updates)
      );
      this.skillsResource.reload();
      return response;
    } catch (err: unknown) {
      this._error.set(err instanceof Error ? err.message : 'Failed to update skill');
      throw err;
    } finally {
      this._loading.set(false);
    }
  }

  /**
   * Delete a skill (soft delete by default).
   */
  async deleteSkill(skillId: string, hard: boolean = false): Promise<void> {
    this._loading.set(true);
    this._error.set(null);
    try {
      await firstValueFrom(
        this.http.delete<void>(`${this.baseUrl()}/${skillId}?hard=${hard}`)
      );
      this.skillsResource.reload();
    } catch (err: unknown) {
      this._error.set(err instanceof Error ? err.message : 'Failed to delete skill');
      throw err;
    } finally {
      this._loading.set(false);
    }
  }

  /**
   * Get roles that grant access to a skill.
   */
  async getSkillRoles(skillId: string): Promise<SkillRoleAssignment[]> {
    const response = await firstValueFrom(
      this.http.get<SkillRolesResponse>(`${this.baseUrl()}/${skillId}/roles`)
    );
    return response.roles;
  }

  /**
   * Set which roles grant access to a skill (bidirectional sync).
   */
  async setSkillRoles(skillId: string, roleIds: string[]): Promise<void> {
    this._loading.set(true);
    this._error.set(null);
    try {
      await firstValueFrom(
        this.http.put(`${this.baseUrl()}/${skillId}/roles`, { appRoleIds: roleIds })
      );
      this.skillsResource.reload();
    } catch (err: unknown) {
      this._error.set(err instanceof Error ? err.message : 'Failed to set skill roles');
      throw err;
    } finally {
      this._loading.set(false);
    }
  }

  // ===========================================================================
  // Reference files (S3-backed supporting reference files — PR-4)
  // ===========================================================================

  /**
   * List a skill's reference-file manifest.
   */
  async listResources(skillId: string): Promise<SkillResourceRef[]> {
    const response = await firstValueFrom(
      this.http.get<SkillResourcesResponse>(`${this.baseUrl()}/${skillId}/resources`)
    );
    return response.resources;
  }

  /**
   * Upload (or replace) one reference file; returns the updated manifest.
   */
  async uploadResource(skillId: string, file: File): Promise<SkillResourceRef[]> {
    const formData = new FormData();
    formData.append('file', file, file.name);
    const response = await firstValueFrom(
      this.http.post<SkillResourcesResponse>(
        `${this.baseUrl()}/${skillId}/resources`,
        formData
      )
    );
    this.skillsResource.reload();
    return response.resources;
  }

  /**
   * Read one reference file's raw text content.
   */
  async readResource(skillId: string, filename: string): Promise<string> {
    return firstValueFrom(
      this.http.get(
        `${this.baseUrl()}/${skillId}/resources/${encodeURIComponent(filename)}`,
        { responseType: 'text' }
      )
    );
  }

  /**
   * Delete one reference file; returns the updated manifest.
   */
  async deleteResource(skillId: string, filename: string): Promise<SkillResourceRef[]> {
    const response = await firstValueFrom(
      this.http.delete<SkillResourcesResponse>(
        `${this.baseUrl()}/${skillId}/resources/${encodeURIComponent(filename)}`
      )
    );
    this.skillsResource.reload();
    return response.resources;
  }

  /**
   * Reload the skills resource.
   */
  reload(): void {
    this.skillsResource.reload();
  }
}
