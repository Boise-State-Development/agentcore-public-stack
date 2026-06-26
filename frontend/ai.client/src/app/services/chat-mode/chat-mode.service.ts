import { Injectable, inject, signal, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../config.service';
import { UserSettingsService } from '../user-settings.service';
import { SessionService } from '../../session/services/session/session.service';

export type ChatMode = 'skill' | 'chat';

/** Admin chat-mode policy from GET /system/chat-settings. */
export interface ChatModePolicy {
  defaultMode: ChatMode;
  allowModeToggle: boolean;
  /**
   * Whether the skills feature is enabled for this environment at all. When
   * false the backend forces tools/chat mode; the SPA also hides the admin
   * skills nav entry. Deferred features report this false.
   */
  skillsEnabled: boolean;
}

/**
 * Pre-load fallback. Defaults skills off so deferred-feature environments
 * never flash the skills surfaces before the policy loads; the server is the
 * source of truth and overrides this on fetch.
 */
const DEFAULT_POLICY: ChatModePolicy = {
  defaultMode: 'chat',
  allowModeToggle: false,
  skillsEnabled: false,
};

/**
 * Holds the user's current agent mode — skills mode (SkillAgent, capabilities
 * come from enabled skills) vs. tools mode (ChatAgent, fine-grained tool
 * toggles) — and the admin policy that governs it.
 *
 * Effective mode precedence: admin policy (when toggling is disallowed) >
 * local selection (hydrated from session preferences or set by the toggle) >
 * user-level preferred mode > admin default. The server enforces the policy
 * independently; this service only mirrors it for the UI.
 */
@Injectable({
  providedIn: 'root'
})
export class ChatModeService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private userSettingsService = inject(UserSettingsService);
  private sessionService = inject(SessionService);

  private _policy = signal<ChatModePolicy>(DEFAULT_POLICY);
  private _policyLoaded = signal(false);

  /**
   * The in-app selection. Hydrated from a session's stored `agentType` when
   * one exists; a session without one (new or pre-feature) inherits the
   * current selection — mode is sticky across pages, like tool preferences.
   */
  private _localMode = signal<ChatMode | null>(null);

  readonly policy = this._policy.asReadonly();
  readonly policyLoaded = this._policyLoaded.asReadonly();

  readonly canToggle = computed(() => this._policy().allowModeToggle);

  /** Whether the skills feature is enabled at all (gates the admin nav entry). */
  readonly skillsEnabled = computed(() => this._policy().skillsEnabled);

  readonly mode = computed<ChatMode>(() => {
    const policy = this._policy();
    if (!policy.allowModeToggle) return policy.defaultMode;
    return this._localMode() ?? this.preferredMode() ?? policy.defaultMode;
  });

  readonly isSkillsMode = computed(() => this.mode() === 'skill');

  /** User-level preferred mode from user settings (null until loaded/set). */
  private readonly preferredMode = computed<ChatMode | null>(() => {
    const settings = this.userSettingsService.settingsResource.hasValue()
      ? this.userSettingsService.settingsResource.value()
      : undefined;
    const preferred = settings?.preferredAgentMode;
    return preferred === 'skill' || preferred === 'chat' ? preferred : null;
  });

  constructor() {
    this.loadPolicy().catch(err => {
      console.error('Failed to load chat-mode policy:', err);
    });
  }

  /** Fetch the admin policy. Falls back to the compiled-in default. */
  async loadPolicy(): Promise<void> {
    try {
      const policy = await firstValueFrom(
        this.http.get<ChatModePolicy>(`${this.config.appApiUrl()}/system/chat-settings`)
      );
      this._policy.set(policy);
    } catch (err) {
      console.error('Chat-mode policy load error (using defaults):', err);
    } finally {
      this._policyLoaded.set(true);
    }
  }

  /**
   * Switch modes. Persists the choice as the user-level preferred mode and,
   * when a session is active, onto that session's preferences so reopening
   * the conversation restores it. Both persists are fire-and-forget — the
   * UI state flips immediately and the server enforces policy regardless.
   */
  setMode(mode: ChatMode, sessionId: string | null = null): void {
    if (!this.canToggle() || this.mode() === mode) return;

    this._localMode.set(mode);

    // Silent: the toggle already applied in-memory; a persistence failure
    // (e.g. user-settings storage not configured) shouldn't raise a dialog.
    this.userSettingsService
      .updateSettings({ preferredAgentMode: mode }, { silent: true })
      .catch(err => console.error('Failed to persist preferred agent mode:', err));

    if (sessionId) {
      this.sessionService
        .updateSessionPreferences(sessionId, { agentType: mode })
        .catch(err => console.error('Failed to persist session agent mode:', err));
    }
  }

  /**
   * Restore the mode a conversation was using when its metadata loads.
   * Called from the session page's hydration effect; ignores sessions
   * without a stored mode so a home-page selection survives into the
   * first message of a new conversation.
   */
  hydrateFromSession(agentType: string | null | undefined): void {
    if (agentType === 'skill' || agentType === 'chat') {
      this._localMode.set(agentType);
    }
  }
}
