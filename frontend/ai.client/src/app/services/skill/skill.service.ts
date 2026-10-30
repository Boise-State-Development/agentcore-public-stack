import { Injectable, inject, signal, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../config.service';
import { ToggleOptions } from '../toggle-options';

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
  /**
   * The runtime's activation key — the name the model sees in
   * `<available_skills>` and the token the composer's `/` menu writes into a
   * message. Served by the backend rather than derived here so the two can
   * never disagree about the slug rule.
   *
   * Optional because the SPA and the backend deploy independently (and in no
   * enforced order): a client that lands ahead of the backend that serves this
   * field must degrade to "no slash commands", not to a menu of `/undefined`.
   */
  slug?: string;
}

/** Response from GET /skills/ */
export interface SkillsResponse {
  skills: UserSkill[];
  totalCount: number;
}

/**
 * One of an Agent's bound skills as the Agent resolved it for this viewer
 * (`GET /agents/{id}/skills`): the shape the lock takes, independent of the
 * user's own `/skills/` list.
 */
export interface AgentLockedSkill {
  skillId: string;
  displayName: string;
  description: string;
  slug: string;
}

/**
 * The skill set an Agent dictates for the active conversation.
 *
 * `ids` are the Agent's bound skill ids. `skills` are those ids resolved by the
 * backend for *this viewer* (names, descriptions and the server's slug), or
 * null while they are still being fetched or when the fetch failed; then the
 * rows are whatever the viewer's own list can name, which is the pre-resolution
 * behaviour and what a backend without the route implies. `resolving` is true
 * only in the gap between locking and the resolved list arriving.
 */
interface AgentSkillLock {
  ids: string[];
  skills: UserSkill[] | null;
  resolving: boolean;
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
 * deferred to the first open of a Customize → Skills page or the composer's
 * skill-command menu (and to an Agent-bound conversation, which needs the
 * names to render locked rows).
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

  /**
   * The load currently in flight, so a second caller *joins* it instead of
   * being told "already loading" and continuing with an empty list. The old
   * `if (this._loading()) return;` guard deduped the request but resolved
   * immediately, which is exactly what let a chat turn sent moments after page
   * load disclose no skills while the next turn disclosed three — a rewritten
   * system prompt on turn 2, paid at the cache-write premium.
   */
  private _inflight: Promise<void> | null = null;

  // Agent Designer: when the active conversation is bound to an Agent that binds
  // skills, the picker is locked to exactly that set — the backend governs skills
  // (and forces skill-mode) at invocation regardless of the client. Null when
  // not agent-bound.
  private readonly _agentLock = signal<AgentSkillLock | null>(null);

  // Public readonly signals
  readonly skills = this._skills.asReadonly();
  readonly loading = this._loading.asReadonly();
  readonly error = this._error.asReadonly();
  readonly initialized = this._initialized.asReadonly();

  /** True when the skill set is dictated by the active Agent and toggles are locked. */
  readonly agentLocked = computed(() => this._agentLock() !== null);

  /**
   * True while an Agent lock is in place but its per-viewer resolution has not
   * arrived. A composer that reads "no skills" in that gap would say something
   * wrong (there are none to run / they are chosen elsewhere), so it waits.
   */
  readonly agentSkillsResolving = computed(() => this._agentLock()?.resolving ?? false);

  // Computed signals
  readonly enabledSkills = computed(() =>
    this._skills().filter(s => s.isEnabled)
  );

  /** Skill ids to send as `enabled_skills` on a skills-mode chat request. */
  readonly enabledSkillIds = computed(() => {
    // Agent-bound: the Agent's skills are the effective set (the backend enforces
    // the same, replace semantics, and forces skill-mode). Toggling is disabled.
    const lock = this._agentLock();
    if (lock !== null) {
      return [...lock.ids];
    }
    return this.enabledSkills().map(s => s.skillId);
  });

  readonly enabledCount = computed(() => {
    const lock = this._agentLock();
    if (lock !== null) {
      return lock.ids.length;
    }
    return this.enabledSkills().length;
  });

  readonly hasSkills = computed(() => this._skills().length > 0);

  /**
   * The skills the picker should render. Agent-locked → only the bound skills
   * (the agent dictates a fixed set, so hide the rest), as the backend resolved
   * them for this viewer once that list is in; otherwise every accessible skill.
   *
   * The resolved list matters because the viewer's own list is their catalog
   * grants and the skills they authored. A skill another member of a project
   * authored is in neither, so intersecting the Agent's ids with it left every
   * member but the author with no rows and no `/` menu.
   */
  readonly visibleSkills = computed(() => {
    const lock = this._agentLock();
    if (lock !== null) {
      return lock.skills ?? this._skills().filter(s => lock.ids.includes(s.skillId));
    }
    return this._skills();
  });

  /**
   * Whether a skill row should render as ON. Agent-locked → membership in the
   * bound set; otherwise the user's own enabled state.
   */
  isSkillShownEnabled(skill: UserSkill): boolean {
    const lock = this._agentLock();
    if (lock !== null) {
      return lock.ids.includes(skill.skillId);
    }
    return skill.isEnabled;
  }

  /**
   * Lock the picker to an Agent's bound skill ids (Agent Designer). Rows are
   * whatever the viewer's own list can name until {@link lockToAgentSkills}
   * supplies the resolved entries; pass `resolving` while that fetch is out.
   */
  lockToAgentSkillIds(skillIds: string[], options?: { resolving?: boolean }): void {
    this._agentLock.set({ ids: [...skillIds], skills: null, resolving: options?.resolving ?? false });
  }

  /**
   * Lock the picker to an Agent's bound skills as the backend resolved them for
   * this viewer (`GET /agents/{id}/skills`): the rows, the `/` commands and the
   * ids all come from these entries, not from the viewer's own list.
   */
  lockToAgentSkills(skills: AgentLockedSkill[]): void {
    this._agentLock.set({
      ids: skills.map(s => s.skillId),
      skills: skills.map(s => ({
        skillId: s.skillId,
        displayName: s.displayName,
        description: s.description,
        category: null,
        userEnabled: null,
        isEnabled: true,
        slug: s.slug,
      })),
      resolving: false,
    });
  }

  /**
   * The per-viewer resolution did not arrive (the route is absent on an older
   * backend, or failed): keep the lock, stop waiting, and let the rows fall
   * back to what the viewer's own list can name.
   */
  settleAgentLock(): void {
    this._agentLock.update(lock => (lock && lock.resolving ? { ...lock, resolving: false } : lock));
  }

  /** Release an Agent skill lock. */
  clearAgentLock(): void {
    this._agentLock.set(null);
  }

  /**
   * Fetch the user's accessible skills. Called on service construction;
   * call again after login or role changes.
   */
  async loadSkills(): Promise<void> {
    // Join an in-flight load rather than returning early: callers await this to
    // know the list is settled.
    if (this._inflight) return this._inflight;

    const inflight = this.fetchSkills();
    this._inflight = inflight;
    try {
      await inflight;
    } finally {
      this._inflight = null;
    }
  }

  private async fetchSkills(): Promise<void> {
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

  /**
   * Toggle a skill's enabled state (optimistic, reverts on save failure).
   *
   * `respectAgentLock` defaults to true — the conversation-scoped behaviour the
   * composer drawer depends on. Global surfaces (Customize) pass `false`; see
   * `services/toggle-options.ts` and `docs/specs/customize-surface.md`
   * §"The agent-lock seam" for why.
   */
  async toggleSkill(skillId: string, options?: ToggleOptions): Promise<void> {
    // Agent-locked: the skill set is dictated by the Agent; ignore toggles.
    if ((options?.respectAgentLock ?? true) && this._agentLock() !== null) return;
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

  /**
   * Resolve once the skill list has settled, starting the load if nothing has.
   *
   * The chat send path awaits this so a turn sent before the (lazily triggered)
   * `/skills/` fetch returns still discloses the same skills a later turn
   * would. Identical disclosure on turn 1 and turn 2 is what keeps the
   * cacheable system-prompt prefix stable across a session.
   *
   * Never rejects: a failed load leaves the picker empty, which is the state
   * the send path already tolerates. It is a no-op once loaded, and joins the
   * in-flight request when one is already running, so awaiting it on every turn
   * costs nothing after the first.
   */
  async ensureLoaded(): Promise<void> {
    if (this._initialized()) return;
    await this.loadSkills().catch(() => undefined);
  }

  /** Reload skills from the server. */
  async reload(): Promise<void> {
    this._initialized.set(false);
    await this.loadSkills();
  }
}
