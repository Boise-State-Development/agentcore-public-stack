import { Injectable, inject, signal, computed, effect } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../config.service';
import { ChatModeService } from '../chat-mode/chat-mode.service';

/**
 * One skill the user's roles grant, as returned by GET /skills/.
 * `userEnabled` is the explicit preference (null = untouched);
 * `isEnabled` is the effective state (untouched skills default to on).
 */
export interface UserSkill {
  skillId: string;
  displayName: string;
  description: string;
  category: string | null;
  boundToolCount: number;
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
 * The skills-mode sibling of ToolService: the backend returns only the
 * ACTIVE skills the user's RBAC roles grant (the same set the SkillAgent
 * can activate), and preferences persist globally per user.
 */
@Injectable({
  providedIn: 'root'
})
export class SkillService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private chatMode = inject(ChatModeService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/skills`);

  /** Guards the one-time auto-load so it fires at most once. */
  private autoLoadTriggered = false;

  // Internal state signals
  private _skills = signal<UserSkill[]>([]);
  private _loading = signal(false);
  private _error = signal<string | null>(null);
  private _initialized = signal(false);

  // Public readonly signals
  readonly skills = this._skills.asReadonly();
  readonly loading = this._loading.asReadonly();
  readonly error = this._error.asReadonly();
  readonly initialized = this._initialized.asReadonly();

  constructor() {
    // Load the user's skills once the feature is known to be enabled. While
    // skills are deferred (disabled) the /skills API is unmounted, so gating
    // on the policy avoids a guaranteed 404 on every session; when enabled,
    // the effect fires as soon as the chat-mode policy resolves.
    effect(() => {
      if (this.chatMode.skillsEnabled() && !this.autoLoadTriggered) {
        this.autoLoadTriggered = true;
        this.loadSkills().catch(err => {
          console.error('Failed to load skills on initialization:', err);
        });
      }
    });
  }

  // Computed signals
  readonly enabledSkills = computed(() =>
    this._skills().filter(s => s.isEnabled)
  );

  /** Skill ids to send as `enabled_skills` on a skills-mode chat request. */
  readonly enabledSkillIds = computed(() =>
    this.enabledSkills().map(s => s.skillId)
  );

  readonly enabledCount = computed(() => this.enabledSkills().length);

  readonly hasSkills = computed(() => this._skills().length > 0);

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
      const message = err instanceof Error ? err.message : 'Failed to load skills';
      this._error.set(message);
      console.error('Skill load error:', err);
    } finally {
      this._loading.set(false);
    }
  }

  /** Toggle a skill's enabled state (optimistic, reverts on save failure). */
  async toggleSkill(skillId: string): Promise<void> {
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
