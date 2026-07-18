import { Injectable, inject, signal, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../config.service';

/**
 * One skill the user can reach (catalog-granted ∪ authored), as returned by
 * GET /skills/. `userEnabled` is the explicit preference (null = untouched);
 * `isEnabled` is the effective state — untouched skills default to **off**
 * (Skills v2 D6 opt-in, the reverse of tools).
 */
export interface UserSkill {
  skillId: string;
  displayName: string;
  description: string;
  category: string | null;
  userEnabled: boolean | null;
  isEnabled: boolean;
}

/** Response from GET /skills/ */
export interface SkillsResponse {
  skills: UserSkill[];
  totalCount: number;
}

/**
 * Service for the user's accessible skills and per-skill preferences.
 *
 * The sibling of ToolService: the backend returns the ACTIVE skills the user
 * can reach (RBAC-granted catalog ∪ skills they authored), and preferences
 * persist globally per user.
 *
 * Unlike ToolService this does NOT load in its constructor. Skills are opt-in
 * and the feature is off in every deployed env until PR-5, so the load is
 * deferred to the first open of the model-settings panel (and to an
 * Agent-bound conversation, which needs the names to render locked rows).
 */
@Injectable({
  providedIn: 'root'
})
export class SkillService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/skills`);

  // Internal state signals
  private _skills = signal<UserSkill[]>([]);
  private _loading = signal(false);
  private _error = signal<string | null>(null);
  private _initialized = signal(false);

  // Agent Designer: when the active conversation is bound to an Agent that binds
  // skills, the picker is locked to exactly that set — the backend governs skills
  // (and forces skill-mode) at invocation regardless of the client. Holds the
  // bound skill ids, or null when not agent-bound.
  private readonly _agentLockedSkillIds = signal<string[] | null>(null);

  // Public readonly signals
  readonly skills = this._skills.asReadonly();
  readonly loading = this._loading.asReadonly();
  readonly error = this._error.asReadonly();
  readonly initialized = this._initialized.asReadonly();

  /** True when the skill set is dictated by the active Agent and toggles are locked. */
  readonly agentLocked = computed(() => this._agentLockedSkillIds() !== null);

  // Computed signals
  readonly enabledSkills = computed(() =>
    this._skills().filter(s => s.isEnabled)
  );

  /** Skill ids to send as `enabled_skills` on a skills-mode chat request. */
  readonly enabledSkillIds = computed(() => {
    // Agent-bound: the Agent's skills are the effective set (the backend enforces
    // the same, replace semantics, and forces skill-mode). Toggling is disabled.
    const locked = this._agentLockedSkillIds();
    if (locked !== null) {
      return [...locked];
    }
    return this.enabledSkills().map(s => s.skillId);
  });

  readonly enabledCount = computed(() => {
    const locked = this._agentLockedSkillIds();
    if (locked !== null) {
      return locked.length;
    }
    return this.enabledSkills().length;
  });

  readonly hasSkills = computed(() => this._skills().length > 0);

  /**
   * The skills the picker should render. Agent-locked → only the bound skills
   * (the agent dictates a fixed set, so hide the rest); otherwise every
   * accessible skill.
   */
  readonly visibleSkills = computed(() => {
    const locked = this._agentLockedSkillIds();
    if (locked !== null) {
      return this._skills().filter(s => locked.includes(s.skillId));
    }
    return this._skills();
  });

  /**
   * Whether a skill row should render as ON. Agent-locked → membership in the
   * bound set; otherwise the user's own enabled state.
   */
  isSkillShownEnabled(skill: UserSkill): boolean {
    const locked = this._agentLockedSkillIds();
    if (locked !== null) {
      return locked.includes(skill.skillId);
    }
    return skill.isEnabled;
  }

  /** Lock the picker to an Agent's bound skills (Agent Designer). */
  lockToAgentSkills(skillIds: string[]): void {
    this._agentLockedSkillIds.set([...skillIds]);
  }

  /** Release an Agent skill lock. */
  clearAgentLock(): void {
    this._agentLockedSkillIds.set(null);
  }

  /**
   * Fetch the user's accessible skills. Called on service construction;
   * call again after login or role changes.
   */
  async loadSkills(): Promise<void> {
    if (this._loading()) return;

    this._loading.set(true);
    this._error.set(null);

    try {
      const response = await firstValueFrom(
        this.http.get<SkillsResponse>(`${this.baseUrl()}/`)
      );

      this._skills.set(response.skills);
      this._initialized.set(true);
    } catch (err: unknown) {
      const status = (err as { status?: number } | null)?.status;
      if (status === 404) {
        // Kill switch off — the `/skills` router isn't mounted when
        // SKILLS_ENABLED is false. Treat it as "no skills" rather than an
        // error: the picker stays hidden (`hasSkills()` is false) and nothing
        // is logged, which is the state of every deployed env until PR-5.
        // Mark initialized so we don't re-probe on every panel open.
        this._skills.set([]);
        this._initialized.set(true);
        return;
      }
      const message = err instanceof Error ? err.message : 'Failed to load skills';
      this._error.set(message);
      console.error('Skill load error:', err);
    } finally {
      this._loading.set(false);
    }
  }

  /** Toggle a skill's enabled state (optimistic, reverts on save failure). */
  async toggleSkill(skillId: string): Promise<void> {
    // Agent-locked: the skill set is dictated by the Agent; ignore toggles.
    if (this._agentLockedSkillIds() !== null) return;
    const skill = this._skills().find(s => s.skillId === skillId);
    if (!skill) return;

    const newState = !skill.isEnabled;

    this._skills.update(skills =>
      skills.map(s =>
        s.skillId === skillId
          ? { ...s, isEnabled: newState, userEnabled: newState }
          : s
      )
    );

    try {
      await firstValueFrom(
        this.http.put(`${this.baseUrl()}/preferences`, {
          preferences: { [skillId]: newState },
        })
      );
    } catch (err) {
      // Revert on error
      this._skills.update(skills =>
        skills.map(s =>
          s.skillId === skillId
            ? { ...s, isEnabled: skill.isEnabled, userEnabled: skill.userEnabled }
            : s
        )
      );
      throw err;
    }
  }

  /** Get a skill by ID. */
  getSkill(skillId: string): UserSkill | undefined {
    return this._skills().find(s => s.skillId === skillId);
  }

  /** Get the list of enabled skill IDs (for non-signal contexts). */
  getEnabledSkillIds(): string[] {
    return this.enabledSkillIds();
  }

  /** Reload skills from the server. */
  async reload(): Promise<void> {
    this._initialized.set(false);
    await this.loadSkills();
  }
}
