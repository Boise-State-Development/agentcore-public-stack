import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { SystemPromptsService } from './system-prompts.service';
import { ConfigService } from '../config.service';
import { SessionService as BffSessionService } from '../../auth/session.service';

/**
 * Regression cover for conversation-mode hydration.
 *
 * The bug: the session page calls `hydrateFromSession` twice on load — once
 * provisionally, before the session's metadata has arrived (to clear whatever
 * the previous conversation had selected), and again with the real value once
 * it lands. The provisional call used to CLAIM the session id, which made the
 * clobber guard reject the real hydration that followed.
 *
 * The damage was not cosmetic. `chat-request.service` sends `selected_prompt_id`
 * from `activePromptId()`, so after any reload the mode silently stopped being
 * applied to every later turn while the stored session preference still said it
 * was on.
 */
describe('SystemPromptsService — session hydration', () => {
  let service: SystemPromptsService;
  let http: HttpTestingController;

  const authed = signal(true);

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        SystemPromptsService,
        { provide: ConfigService, useValue: { appApiUrl: signal('/api') } },
        { provide: BffSessionService, useValue: { isAuthenticated: authed } },
      ],
    });
    service = TestBed.inject(SystemPromptsService);
    http = TestBed.inject(HttpTestingController);
    http.match(req => req.url.includes('system-prompts')).forEach(req =>
      req.flush({ prompts: [], total: 0 }),
    );
  });

  afterEach(() => {
    http.match(() => true);
    TestBed.resetTestingModule();
    vi.restoreAllMocks();
  });

  it('applies the stored selection after a provisional clear', () => {
    // The reload sequence, in order.
    service.hydrateFromSession('sess-1', null, false); // metadata not here yet
    service.hydrateFromSession('sess-1', 'mode-a'); // metadata lands

    expect(service.activePromptId()).toBe('mode-a');
  });

  it('regression: the provisional call must not claim the session', () => {
    // With `claim` defaulted to true on the provisional call — the old
    // behaviour — the second hydration is rejected and the mode is lost.
    service.hydrateFromSession('sess-1', null); // claims sess-1
    service.hydrateFromSession('sess-1', 'mode-a'); // blocked by the guard

    expect(service.activePromptId()).toBeNull();
  });

  it('still protects a just-made choice from a stale metadata read', () => {
    // The reason the guard exists: the user picks a mode, and metadata written
    // before that choice arrives a moment later.
    service.hydrateFromSession('sess-1', 'mode-a');
    expect(service.activePromptId()).toBe('mode-a');

    service.hydrateFromSession('sess-1', 'mode-stale');
    expect(service.activePromptId()).toBe('mode-a');
  });

  it('protects a deliberate None from a stale read too', () => {
    // The edge the fix must not break: turning a mode OFF is a claim as much as
    // turning one on, so the previous id must not come back.
    service.hydrateFromSession('sess-1', null); // claimed: user chose None
    service.hydrateFromSession('sess-1', 'mode-a'); // stale metadata

    expect(service.activePromptId()).toBeNull();
  });

  it('does not leak one conversation’s mode into the next', () => {
    service.hydrateFromSession('sess-1', 'mode-a');
    service.hydrateFromSession('sess-2', null, false); // navigating away
    expect(service.activePromptId()).toBeNull();
  });

  it('lets a new session hydrate after an unclaimed clear', () => {
    service.hydrateFromSession('sess-1', 'mode-a');
    service.hydrateFromSession('sess-2', null, false);
    service.hydrateFromSession('sess-2', 'mode-b');
    expect(service.activePromptId()).toBe('mode-b');
  });
});
