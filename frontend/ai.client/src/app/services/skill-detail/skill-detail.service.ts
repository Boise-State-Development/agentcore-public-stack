import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../config.service';
import { SkillResourceKind } from '../../my-skills/models/my-skill.model';

/**
 * One supporting file on a skill's agentskills.io bundle. Bytes live in S3;
 * this is the manifest entry the skill row carries.
 */
export interface SkillDetailResource {
  filename: string;
  contentHash: string;
  size: number;
  contentType: string;
  s3Key: string;
  kind: SkillResourceKind;
}

/**
 * One skill the user can reach, with everything `GET /skills/` leaves out.
 * Mirrors the backend `SkillDetailResponse` in
 * `apis/app_api/skills/routes.py` (CLAUDE.md cross-package contract).
 *
 * ⚠️ There is deliberately no `ownerId` and no `allowedAppRoles`: the first
 * would name another user, and the second is an admin-display projection of
 * RBAC. `isOwned` is the only part of ownership this surface needs.
 */
export interface SkillDetail {
  skillId: string;
  displayName: string;
  description: string;
  /** The SKILL.md body — level 2 of the progressive disclosure. */
  instructions: string;
  /** skill_ids folded into this one (composite skills). */
  compose: string[];
  /** Advisory only: parsed from frontmatter, never enforced (Skills v2 D4). */
  allowedTools: string[];
  /** Frontmatter passthrough (license, compatibility, arbitrary keys). */
  skillMetadata: Record<string, unknown>;
  resources: SkillDetailResource[];
  status: string;
  category: string | null;
  userEnabled: boolean | null;
  isEnabled: boolean;
  /** True when the caller authored it — drives the "Edit in My Skills" link. */
  isOwned: boolean;
  createdAt: string | null;
  updatedAt: string | null;
}

type Entry =
  | { state: 'loading' }
  | { state: 'loaded'; value: SkillDetail }
  | { state: 'missing' }
  | { state: 'failed' };

/**
 * Reads one accessible skill's full record.
 *
 * Its own endpoint rather than a fatter `GET /skills/`: that call is a
 * first-load payload for every granted skill, and a SKILL.md body per row
 * would be paid on every load to render a list that shows neither. This is
 * fetched once, for the one skill a user opened.
 *
 * Cached per skill for the life of the page. `missing` is kept distinct from
 * `failed` because a 404 here means "retired, or your roles no longer grant
 * it" — a real answer the page renders — while `failed` is a transport fault,
 * and both are remembered rather than retried, so a dead endpoint cannot turn
 * a re-render into a request loop.
 */
@Injectable({ providedIn: 'root' })
export class SkillDetailService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);

  private readonly entries = signal<Record<string, Entry>>({});

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/skills`);

  /** The detail for a skill, or null while loading / missing / failed. */
  detailFor(skillId: string): SkillDetail | null {
    const entry = this.entries()[skillId];
    return entry?.state === 'loaded' ? entry.value : null;
  }

  isLoading(skillId: string): boolean {
    return this.entries()[skillId]?.state === 'loading';
  }

  /** Resolved, and the skill is not reachable by this user. */
  isMissing(skillId: string): boolean {
    return this.entries()[skillId]?.state === 'missing';
  }

  hasFailed(skillId: string): boolean {
    return this.entries()[skillId]?.state === 'failed';
  }

  /** Fetch once per skill. A resolved entry of any kind is never re-fetched. */
  async ensure(skillId: string): Promise<void> {
    if (!skillId || this.entries()[skillId]) return;
    this.entries.update(current => ({ ...current, [skillId]: { state: 'loading' } }));
    try {
      const value = await firstValueFrom(
        this.http.get<SkillDetail>(
          `${this.baseUrl()}/${encodeURIComponent(skillId)}`,
          { withCredentials: true },
        ),
      );
      this.entries.update(current => ({
        ...current,
        [skillId]: { state: 'loaded', value },
      }));
    } catch (err: unknown) {
      const status = (err as { status?: number } | null)?.status;
      this.entries.update(current => ({
        ...current,
        [skillId]: { state: status === 404 ? 'missing' : 'failed' },
      }));
    }
  }

  /**
   * Where a supporting file's bytes are served from. Access-scoped, not
   * owner-scoped, so a catalog skill's reference files open for anyone the
   * skill is granted to. Served `attachment` + `nosniff` by the backend.
   */
  resourceUrl(skillId: string, filename: string): string {
    return `${this.baseUrl()}/${encodeURIComponent(skillId)}/resources/${encodeURIComponent(filename)}`;
  }

  /** Drop a cached record so the next `ensure` re-reads it. */
  invalidate(skillId: string): void {
    this.entries.update(current => {
      if (!(skillId in current)) return current;
      const next = { ...current };
      delete next[skillId];
      return next;
    });
  }
}
