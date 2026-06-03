import { Injectable, inject, signal, computed, effect, untracked } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../config.service';
import { SessionService as BffSessionService } from '../../auth/session.service';
import { SessionService } from '../../session/services/session/session.service';

export interface SystemPrompt {
  prompt_id: string;
  name: string;
  description: string;
}

export interface SystemPromptsResponse {
  prompts: SystemPrompt[];
  total: number;
}

/**
 * Service for loading the admin-managed system prompt catalog and tracking
 * the active prompt for the current conversation.
 *
 * Users see name + description only — prompt text is server-side only.
 *
 * Loaded lazily after the BFF session bootstrap reports an authenticated
 * user. Eager loading would 401 on every login/logout transition for any
 * user who lands on an unauthenticated route first.
 */
@Injectable({ providedIn: 'root' })
export class SystemPromptsService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly bffSession = inject(BffSessionService);
  private readonly sessionService = inject(SessionService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/system-prompts`);

  private readonly _prompts = signal<SystemPrompt[]>([]);
  private readonly _loading = signal(false);
  private readonly _error = signal<string | null>(null);
  private readonly _activePromptId = signal<string | null>(null);
  /**
   * Tracks which session the active prompt is bound to. ``null`` means the
   * selection was made before any session existed (home page) — once a
   * session ID is assigned via the next submit, the prompt is "claimed"
   * by that session. Used to decide whether incoming session metadata
   * should override the local selection.
   */
  private readonly _activePromptSessionId = signal<string | null>(null);

  readonly prompts = this._prompts.asReadonly();
  readonly loading = this._loading.asReadonly();
  readonly error = this._error.asReadonly();
  readonly activePromptId = this._activePromptId.asReadonly();

  readonly activePrompt = computed(() =>
    this._prompts().find(p => p.prompt_id === this._activePromptId()) ?? null
  );

  readonly hasPrompts = computed(() => this._prompts().length > 0);

  constructor() {
    // Load on first auth, and reload again if the user logs back in after
    // a logout. The body runs inside `untracked` so the loader's reads
    // and writes to `_loading` / `_prompts` don't retrigger this effect.
    effect(() => {
      const authed = this.bffSession.isAuthenticated();
      untracked(() => {
        if (authed) {
          this.loadPrompts().catch(err => {
            console.error('Failed to load system prompts:', err);
          });
        } else {
          // Reset so a re-login refetches rather than serving stale data.
          this._prompts.set([]);
          this._activePromptId.set(null);
          this._activePromptSessionId.set(null);
        }
      });
    });
  }

  async loadPrompts(): Promise<void> {
    if (this._loading()) return;
    this._loading.set(true);
    this._error.set(null);
    try {
      const response = await firstValueFrom(
        this.http.get<SystemPromptsResponse>(`${this.baseUrl()}/`)
      );
      this._prompts.set(response.prompts);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to load system prompts';
      this._error.set(message);
      console.error('System prompts load error:', err);
    } finally {
      this._loading.set(false);
    }
  }

  /**
   * Set the active prompt for the current session and persist to the BFF.
   * Pass `null` to clear. Both UI sites (settings panel + chat-input chip)
   * route through here so the wire convention lives in one place.
   *
   * The BFF treats `selectedPromptId: null` as an explicit clear and an
   * omitted field as "leave unchanged".
   *
   * The selection is bound to ``sessionId`` so subsequent metadata
   * refreshes for the same session (or staged → real session promotion
   * on first submit) don't wipe the user's choice.
   */
  async setActivePrompt(sessionId: string | null, promptId: string | null): Promise<void> {
    this._activePromptId.set(promptId);
    this._activePromptSessionId.set(sessionId);
    if (!sessionId) return;
    await this.sessionService.updateSessionPreferences(sessionId, {
      selectedPromptId: promptId,
    });
  }

  /**
   * Hydrate the active prompt ID from persisted session preferences.
   * Called by the session page when loading or switching sessions.
   *
   * Skips when the local selection is already bound to this session —
   * otherwise a stale metadata read mid-persist could clobber the user's
   * just-made choice. The local ``_activePromptId`` is the source of
   * truth for the session it was claimed against.
   */
  hydrateFromSession(sessionId: string | null, selectedPromptId: string | null): void {
    if (sessionId && this._activePromptSessionId() === sessionId) {
      // Already claimed by this session locally — server is catching up.
      return;
    }
    this._activePromptId.set(selectedPromptId);
    this._activePromptSessionId.set(sessionId);
  }

  /**
   * Promote a home-page selection (sessionId = null) onto a freshly
   * created session. Called by ChatRequestService after generating the
   * new session id but before the first submit fires, so the local
   * selection is correctly bound to the new session for hydrate logic.
   */
  bindToSession(sessionId: string): void {
    if (this._activePromptSessionId() === null && this._activePromptId() !== null) {
      this._activePromptSessionId.set(sessionId);
    }
  }

  async reload(): Promise<void> {
    this._loading.set(false);
    await this.loadPrompts();
  }
}
