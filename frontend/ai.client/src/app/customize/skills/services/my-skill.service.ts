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
export class MySkillService {
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

  async deleteSkill(skillId: string): Promise<void> {
    this.error.set(null);
    try {
      await firstValueFrom(
        this.http.delete(`${this.baseUrl()}/${skillId}`, { withCredentials: true }),
      );
      this.skills.update((current) => current.filter((s) => s.skillId !== skillId));
      this.agents.invalidateBindable('skill');
    } catch (err) {
      this.error.set(this.messageFor(err, 'Failed to delete the skill'));
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
      this.http.get(`${this.baseUrl()}/${skillId}/resources/${encodeURIComponent(filename)}`, {
        withCredentials: true,
        responseType: 'text',
      }),
    );
  }

  async deleteResource(skillId: string, filename: string): Promise<MySkillResourceRef[]> {
    const response = await firstValueFrom(
      this.http.delete<MySkillResourcesResponse>(
        `${this.baseUrl()}/${skillId}/resources/${encodeURIComponent(filename)}`,
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
    const form = importForm(input);
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
    const form = importForm(input);
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
    options: { expectedBundleHash?: string | null; expectedUpdatedAt?: string | null },
  ): Promise<ImportApplyResult> {
    const form = importForm(input);
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

  /** The backend's message for a failed call, or `fallback`. */
  describeError(err: unknown, fallback: string): string {
    return this.messageFor(err, fallback);
  }

  /**
   * Prefer the backend's own message (FastAPI puts it on `error.detail`) so a
   * cap or validation failure reads as written, not as a generic HTTP error.
   */
  private messageFor(err: unknown, fallback: string): string {
    const detail = (err as { error?: { detail?: unknown } } | null)?.error?.detail;
    if (typeof detail === 'string' && detail.trim()) {
      return detail;
    }
    return err instanceof Error ? err.message : fallback;
  }
}

function importForm(input: SkillImportInput): FormData {
  const form = new FormData();
  if (input.file) {
    form.append('file', input.file, input.file.name);
  } else if (input.url) {
    form.append('url', input.url.trim());
  }
  if (input.path !== undefined && input.path !== null) {
    form.append('path', input.path);
  }
  return form;
}
