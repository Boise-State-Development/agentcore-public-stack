import { HttpClient } from '@angular/common/http';
import { Injectable, computed, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../services/config.service';
import {
  CreateMySkillRequest,
  MySkill,
  MySkillListResponse,
  MySkillResourceRef,
  MySkillResourcesResponse,
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
 */
@Injectable({ providedIn: 'root' })
export class MySkillService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

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
    } catch (err) {
      this.error.set(this.messageFor(err, 'Failed to delete the skill'));
      throw err;
    }
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
