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
import { SkillMdParseResult } from '../../../shared/skills/skill-md-parse';
import {
  SkillRestoreResult,
  SkillVersionDetail,
  SkillVersionHistoryApi,
  SkillVersionList,
} from '../../../shared/skills/skill-version-history.model';
import {
  ImportApplyResult,
  ImportPreview,
  SkillBundleImportApi,
  SkillImportInput,
  describeSkillApiError,
  skillImportForm,
} from '../../../shared/skills/skill-bundle-import.model';
import { encodeSkillFilePath } from '../../../shared/utils/skill-resource-path';

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
export class AdminSkillService
  implements SkillBundleImportApi<AdminSkill>, SkillVersionHistoryApi<AdminSkill>
{
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
   * Read one SKILL.md into create-form fields and diagnostics. Writes nothing;
   * the server owns the format (one parser for every ingest path).
   */
  async parseSkillMd(file: File): Promise<SkillMdParseResult> {
    const form = new FormData();
    form.append('file', file, file.name);
    return firstValueFrom(
      this.http.post<SkillMdParseResult>(`${this.baseUrl()}/import/parse`, form),
    );
  }

  // ===========================================================================
  // Bundle import (skill-bundle-import.md) — the catalog twin of My Skills'
  // import, rendered by the shared SkillBundleImportComponent.
  // ===========================================================================

  /** What importing `input` would do, against catalog skill `skillId` when given. Writes nothing. */
  previewImport(input: SkillImportInput, skillId?: string | null): Promise<ImportPreview> {
    const form = skillImportForm(input);
    if (skillId) {
      form.append('skillId', skillId);
    }
    return firstValueFrom(this.http.post<ImportPreview>(`${this.baseUrl()}/import/preview`, form));
  }

  /** Create a draft catalog skill from a bundle. `expectedBundleHash` is the preview's. */
  async importSkill(
    input: SkillImportInput,
    options: { displayName?: string; expectedBundleHash?: string | null },
  ): Promise<ImportApplyResult<AdminSkill>> {
    const form = skillImportForm(input);
    if (options.displayName) {
      form.append('displayName', options.displayName);
    }
    if (options.expectedBundleHash) {
      form.append('expectedBundleHash', options.expectedBundleHash);
    }
    const result = await firstValueFrom(
      this.http.post<ImportApplyResult<AdminSkill>>(`${this.baseUrl()}/import`, form),
    );
    this.skillsResource.reload();
    return result;
  }

  /**
   * Re-import a bundle into a catalog skill: replaces its description,
   * instructions and files, keeps its name, status, category and grants.
   * `confirmReach` is a My Skills concept and is not sent.
   */
  async reimportSkill(
    skillId: string,
    input: SkillImportInput,
    options: { expectedBundleHash?: string | null; expectedUpdatedAt?: string | null },
  ): Promise<ImportApplyResult<AdminSkill>> {
    const form = skillImportForm(input);
    if (options.expectedBundleHash) {
      form.append('expectedBundleHash', options.expectedBundleHash);
    }
    if (options.expectedUpdatedAt) {
      form.append('expectedUpdatedAt', options.expectedUpdatedAt);
    }
    const result = await firstValueFrom(
      this.http.post<ImportApplyResult<AdminSkill>>(
        `${this.baseUrl()}/${encodeURIComponent(skillId)}/import`,
        form,
      ),
    );
    this.skillsResource.reload();
    return result;
  }

  // ===========================================================================
  // Version history (skill-version-history.md §6) — the catalog twin of My
  // Skills', rendered by the shared SkillVersionHistoryComponent.
  // ===========================================================================

  /** A page of a catalog skill's history, newest first. */
  listVersions(
    skillId: string,
    options: { limit?: number; before?: number | null } = {},
  ): Promise<SkillVersionList> {
    const params: Record<string, string> = {};
    if (options.limit) params['limit'] = String(options.limit);
    if (options.before) params['before'] = String(options.before);
    return firstValueFrom(
      this.http.get<SkillVersionList>(`${this.baseUrl()}/${encodeURIComponent(skillId)}/versions`, {
        params,
      }),
    );
  }

  /** One version, with the change from the live skill to it. */
  getVersion(skillId: string, version: number): Promise<SkillVersionDetail> {
    return firstValueFrom(
      this.http.get<SkillVersionDetail>(
        `${this.baseUrl()}/${encodeURIComponent(skillId)}/versions/${version}`,
      ),
    );
  }

  /** Put a version back as a new version. No reach confirmation, as for an admin edit. */
  async restoreVersion(
    skillId: string,
    version: number,
    options: { expectedUpdatedAt?: string | null },
  ): Promise<SkillRestoreResult<AdminSkill>> {
    const result = await firstValueFrom(
      this.http.post<SkillRestoreResult<AdminSkill>>(
        `${this.baseUrl()}/${encodeURIComponent(skillId)}/versions/${version}/restore`,
        { expectedUpdatedAt: options.expectedUpdatedAt ?? null },
      ),
    );
    this.skillsResource.reload();
    return result;
  }

  /** The backend's message for a failed call, or `fallback`. */
  describeError(err: unknown, fallback: string): string {
    return describeSkillApiError(err, fallback);
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
   * A catalog skill as an agentskills.io bundle (`{slug}.zip`), optionally a
   * frozen version. A user-authored skill is a 404 on this route.
   */
  async exportSkill(skillId: string, version?: number): Promise<Blob> {
    const params: Record<string, string> = version ? { version: String(version) } : {};
    return firstValueFrom(
      this.http.get(`${this.baseUrl()}/${encodeURIComponent(skillId)}/export`, {
        params,
        responseType: 'blob',
      }),
    );
  }

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
        `${this.baseUrl()}/${skillId}/resources/${encodeSkillFilePath(filename)}`,
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
        `${this.baseUrl()}/${skillId}/resources/${encodeSkillFilePath(filename)}`
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
