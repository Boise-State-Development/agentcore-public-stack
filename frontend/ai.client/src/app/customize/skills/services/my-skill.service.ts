import { HttpClient } from '@angular/common/http';
import { Injectable, computed, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { AgentService } from '../../../agents/services/agent.service';
import { ConfigService } from '../../../services/config.service';
import { SkillMdParseResult } from '../../../shared/skills/skill-md-parse';
import {
  CreateMySkillRequest,
  ImportApplyResult,
  ImportPreview,
  MySkill,
  MySkillListResponse,
  MySkillResourceRef,
  MySkillResourcesResponse,
  SkillImportInput,
  SkillResourceKind,
  UpdateMySkillRequest,
} from '../models/my-skill.model';
import { encodeSkillFilePath } from '../../../shared/utils/skill-resource-path';
import { reachConfirmationFrom } from '../../../shared/skills/skill-provenance';
import {
  SkillUseTarget,
  SkillUseUpdateResult,
  SkillUses,
  SkillUsesApi,
} from '../../../shared/skills/skill-uses.model';
import {
  SkillRestoreResult,
  SkillVersionDetail,
  SkillVersionHistoryApi,
  SkillVersionList,
} from '../../../shared/skills/skill-version-history.model';
import {
  SkillBundleImportApi,
  describeSkillApiError,
  skillImportForm,
} from '../../../shared/skills/skill-bundle-import.model';

/**
 * Signal-based state for the user-authored skills tier (Skills v2 PR-3).
 *
 * Mirrors the memory-spaces / schedules facades: private mutable signals,
 * readonly public views, async methods that keep local state in sync.
 *
 * `accessible$` rides the list call the way `MemorySpaceService.accessible$`
 * rides spaces: a successful (even empty) list means the `SKILLS_ENABLED` kill
 * switch is on; a 404 flips it false so the nav entry and page hide gracefully
 * rather than surfacing an error. `null` (unresolved) also hides, so nothing
 * flashes in before disappearing.
 *
 * Every write also drops the Agent Designer's memoised `skill` palette: an
 * authored skill is part of the caller's accessible set (`resolve_accessible_skill_ids`
 * unions catalog grants with ownership), so a project's Skills page or an Agent form
 * that fetched the palette before the save must re-fetch it, or the new skill is
 * missing there until a reload. A user report found exactly that gap.
 */
@Injectable({ providedIn: 'root' })
export class MySkillService
  implements SkillBundleImportApi<MySkill>, SkillVersionHistoryApi<MySkill>, SkillUsesApi
{
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private agents = inject(AgentService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/skills/mine`);

  private skills = signal<MySkill[]>([]);
  private loading = signal<boolean>(false);
  private error = signal<string | null>(null);
  private accessible = signal<boolean | null>(null);

  readonly skills$ = this.skills.asReadonly();
  readonly loading$ = this.loading.asReadonly();
  readonly error$ = this.error.asReadonly();
  readonly accessible$ = this.accessible.asReadonly();

  async loadSkills(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);

    try {
      const response = await firstValueFrom(
        this.http.get<MySkillListResponse>(this.baseUrl(), { withCredentials: true }),
      );
      this.skills.set(response?.skills ?? []);
      this.accessible.set(true);
    } catch (err: unknown) {
      const status = (err as { status?: number } | null)?.status;
      if (status === 404) {
        // Kill switch off — fail gracefully rather than surfacing an error.
        this.accessible.set(false);
        this.skills.set([]);
        return;
      }
      this.error.set(this.messageFor(err, 'Failed to load your skills'));
      throw err;
    } finally {
      this.loading.set(false);
    }
  }

  getSkill(skillId: string): Promise<MySkill> {
    return firstValueFrom(
      this.http.get<MySkill>(`${this.baseUrl()}/${skillId}`, { withCredentials: true }),
    );
  }

  async createSkill(request: CreateMySkillRequest): Promise<MySkill> {
    this.error.set(null);
    try {
      const skill = await firstValueFrom(
        this.http.post<MySkill>(this.baseUrl(), request, { withCredentials: true }),
      );
      this.skills.update((current) => [...current, skill]);
      this.agents.invalidateBindable('skill');
      return skill;
    } catch (err) {
      this.error.set(this.messageFor(err, 'Failed to create the skill'));
      throw err;
    }
  }

  async updateSkill(skillId: string, request: UpdateMySkillRequest): Promise<MySkill> {
    this.error.set(null);
    try {
      const skill = await firstValueFrom(
        this.http.put<MySkill>(`${this.baseUrl()}/${skillId}`, request, {
          withCredentials: true,
        }),
      );
      this.skills.update((current) =>
        current.map((s) => (s.skillId === skillId ? skill : s)),
      );
      this.agents.invalidateBindable('skill');
      return skill;
    } catch (err) {
      this.error.set(this.messageFor(err, 'Failed to save the skill'));
      throw err;
    }
  }

  /**
   * Delete an authored skill. While a project or shared agent uses it, the server
   * answers 409 with who loses it; that error is rethrown without the banner so
   * the caller can ask, then call again with `confirmReach`.
   */
  async deleteSkill(skillId: string, options: { confirmReach?: boolean } = {}): Promise<void> {
    this.error.set(null);
    try {
      await firstValueFrom(
        this.http.delete(`${this.baseUrl()}/${skillId}`, {
          withCredentials: true,
          params: options.confirmReach ? { confirmReach: 'true' } : {},
        }),
      );
      this.skills.update((current) => current.filter((s) => s.skillId !== skillId));
      this.agents.invalidateBindable('skill');
    } catch (err) {
      if (reachConfirmationFrom(err) === null) {
        this.error.set(this.messageFor(err, 'Failed to delete the skill'));
      }
      throw err;
    }
  }

  /**
   * The owned skill as an agentskills.io bundle (`{slug}.zip`), optionally a
   * frozen version. Owner only on the server: anything else is a 404.
   */
  exportSkill(skillId: string, version?: number): Promise<Blob> {
    const params: Record<string, string> = version ? { version: String(version) } : {};
    return firstValueFrom(
      this.http.get(`${this.baseUrl()}/${encodeURIComponent(skillId)}/export`, {
        params,
        responseType: 'blob',
        withCredentials: true,
      }),
    );
  }

  // ---- bundle files ------------------------------------------------------

  async uploadResource(
    skillId: string,
    file: File,
    kind: SkillResourceKind = 'reference',
  ): Promise<MySkillResourceRef[]> {
    const form = new FormData();
    form.append('file', file, file.name);
    form.append('kind', kind);

    const response = await firstValueFrom(
      this.http.post<MySkillResourcesResponse>(
        `${this.baseUrl()}/${skillId}/resources`,
        form,
        { withCredentials: true },
      ),
    );
    return response?.resources ?? [];
  }

  async readResource(skillId: string, filename: string): Promise<string> {
    return firstValueFrom(
      this.http.get(`${this.baseUrl()}/${skillId}/resources/${encodeSkillFilePath(filename)}`, {
        withCredentials: true,
        responseType: 'text',
      }),
    );
  }

  async deleteResource(skillId: string, filename: string): Promise<MySkillResourceRef[]> {
    const response = await firstValueFrom(
      this.http.delete<MySkillResourcesResponse>(
        `${this.baseUrl()}/${skillId}/resources/${encodeSkillFilePath(filename)}`,
        { withCredentials: true },
      ),
    );
    return response?.resources ?? [];
  }

  // ---- SKILL.md import ------------------------------------------------------

  /**
   * Read one SKILL.md into form fields and diagnostics. Writes nothing; the
   * server owns the format (one parser for every ingest path).
   */
  parseSkillMd(file: File): Promise<SkillMdParseResult> {
    const form = new FormData();
    form.append('file', file, file.name);
    return firstValueFrom(
      this.http.post<SkillMdParseResult>(`${this.baseUrl()}/import/parse`, form, {
        withCredentials: true,
      }),
    );
  }

  // ---- bundle import (skill-bundle-import.md) ------------------------------

  /** What importing `input` would do, against `skillId` when given. Writes nothing. */
  previewImport(input: SkillImportInput, skillId?: string | null): Promise<ImportPreview> {
    const form = skillImportForm(input);
    if (skillId) {
      form.append('skillId', skillId);
    }
    return firstValueFrom(
      this.http.post<ImportPreview>(`${this.baseUrl()}/import/preview`, form, {
        withCredentials: true,
      }),
    );
  }

  /** Create a skill from a bundle. `expectedBundleHash` is the preview's. */
  async importSkill(
    input: SkillImportInput,
    options: { displayName?: string; expectedBundleHash?: string | null },
  ): Promise<ImportApplyResult> {
    const form = skillImportForm(input);
    if (options.displayName) {
      form.append('displayName', options.displayName);
    }
    if (options.expectedBundleHash) {
      form.append('expectedBundleHash', options.expectedBundleHash);
    }
    const result = await firstValueFrom(
      this.http.post<ImportApplyResult>(`${this.baseUrl()}/import`, form, {
        withCredentials: true,
      }),
    );
    this.skills.update((current) => [...current, result.skill]);
    this.agents.invalidateBindable('skill');
    return result;
  }

  /**
   * Re-import a bundle into an owned skill. The backend writes the live skill
   * only and never cuts a version, so a project pinned to one keeps it until
   * someone clicks Update on the project's Skills page.
   */
  async reimportSkill(
    skillId: string,
    input: SkillImportInput,
    options: {
      expectedBundleHash?: string | null;
      expectedUpdatedAt?: string | null;
      /** Sent after the user confirmed the reach a 409 reported. */
      confirmReach?: boolean;
    },
  ): Promise<ImportApplyResult> {
    const form = skillImportForm(input);
    if (options.confirmReach) {
      form.append('confirmReach', 'true');
    }
    if (options.expectedBundleHash) {
      form.append('expectedBundleHash', options.expectedBundleHash);
    }
    if (options.expectedUpdatedAt) {
      form.append('expectedUpdatedAt', options.expectedUpdatedAt);
    }
    const result = await firstValueFrom(
      this.http.post<ImportApplyResult>(`${this.baseUrl()}/${skillId}/import`, form, {
        withCredentials: true,
      }),
    );
    this.skills.update((current) =>
      current.map((s) => (s.skillId === skillId ? result.skill : s)),
    );
    this.agents.invalidateBindable('skill');
    return result;
  }

  // ---- version history (skill-version-history.md §6) ----------------------

  /** A page of an owned skill's history, newest first. */
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
        withCredentials: true,
      }),
    );
  }

  /** One version, with the change from the live skill to it. */
  getVersion(skillId: string, version: number): Promise<SkillVersionDetail> {
    return firstValueFrom(
      this.http.get<SkillVersionDetail>(
        `${this.baseUrl()}/${encodeURIComponent(skillId)}/versions/${version}`,
        { withCredentials: true },
      ),
    );
  }

  /**
   * Put a version back as a new version. 409s with the reach summary until the
   * user confirms, as an edit does; pins do not move.
   */
  async restoreVersion(
    skillId: string,
    version: number,
    options: { expectedUpdatedAt?: string | null; confirmReach?: boolean },
  ): Promise<SkillRestoreResult<MySkill>> {
    const result = await firstValueFrom(
      this.http.post<SkillRestoreResult<MySkill>>(
        `${this.baseUrl()}/${encodeURIComponent(skillId)}/versions/${version}/restore`,
        {
          expectedUpdatedAt: options.expectedUpdatedAt ?? null,
          confirmReach: options.confirmReach ?? false,
        },
        { withCredentials: true },
      ),
    );
    this.skills.update((current) =>
      current.map((s) => (s.skillId === skillId ? result.skill : s)),
    );
    this.agents.invalidateBindable('skill');
    return result;
  }

  // ---- where it's used (skill-version-history.md D9) -----------------------

  /** The owner's Agents and projects that bind the skill, current and behind. */
  getUses(skillId: string): Promise<SkillUses> {
    return firstValueFrom(
      this.http.get<SkillUses>(`${this.baseUrl()}/${encodeURIComponent(skillId)}/uses`, {
        withCredentials: true,
      }),
    );
  }

  /** Move the named pins to the skill's current content; one result per target. */
  async updateUses(skillId: string, targets: SkillUseTarget[]): Promise<SkillUseUpdateResult[]> {
    const response = await firstValueFrom(
      this.http.post<{ results: SkillUseUpdateResult[] }>(
        `${this.baseUrl()}/${encodeURIComponent(skillId)}/uses/update`,
        { targets },
        { withCredentials: true },
      ),
    );
    if (response.results.some((r) => r.ok && r.kind === 'agent')) {
      this.agents.invalidateBindable('skill');
    }
    return response.results;
  }

  /** The backend's message for a failed call, or `fallback`. */
  describeError(err: unknown, fallback: string): string {
    return this.messageFor(err, fallback);
  }

  /**
   * Prefer the backend's own message (FastAPI puts it on `error.detail`) so a
   * cap or validation failure reads as written, not as a generic HTTP error.
   */
  private messageFor(err: unknown, fallback: string): string {
    return describeSkillApiError(err, fallback);
  }
}
