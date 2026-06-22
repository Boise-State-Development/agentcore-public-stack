import { describe, it, expect, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { ChatModeService, ChatModePolicy } from './chat-mode.service';
import { ConfigService } from '../config.service';
import { UserSettingsService, UserSettings } from '../user-settings.service';
import { SessionService } from '../../session/services/session/session.service';

describe('ChatModeService', () => {
  let service: ChatModeService;
  let httpMock: HttpTestingController;

  let userSettings: UserSettings | undefined;
  const updateSettings = vi.fn(async (s: Partial<UserSettings>) => s as UserSettings);
  const updateSessionPreferences = vi.fn(async () => ({}));

  const fakeUserSettingsService = {
    settingsResource: {
      hasValue: () => userSettings !== undefined,
      value: () => userSettings,
      reload: () => undefined,
    },
    updateSettings,
  };

  const fakeSessionService = { updateSessionPreferences };

  async function setup(policy: ChatModePolicy, settings?: UserSettings) {
    userSettings = settings;
    updateSettings.mockClear();
    updateSessionPreferences.mockClear();

    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        ChatModeService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
        { provide: UserSettingsService, useValue: fakeUserSettingsService },
        { provide: SessionService, useValue: fakeSessionService },
      ],
    });

    service = TestBed.inject(ChatModeService);
    httpMock = TestBed.inject(HttpTestingController);

    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/system/chat-settings').flush(policy);
    });
  }

  afterEach(() => {
    TestBed.resetTestingModule();
    httpMock.match(() => true);
  });

  it('defaults to the admin default mode', async () => {
    await setup({ defaultMode: 'skill', allowModeToggle: true, skillsEnabled: true });
    expect(service.mode()).toBe('skill');
    expect(service.isSkillsMode()).toBe(true);
    expect(service.canToggle()).toBe(true);
    expect(service.skillsEnabled()).toBe(true);
  });

  it('reflects skillsEnabled=false from the policy (deferred feature)', async () => {
    await setup({ defaultMode: 'chat', allowModeToggle: false, skillsEnabled: false });
    expect(service.skillsEnabled()).toBe(false);
    expect(service.mode()).toBe('chat');
    expect(service.canToggle()).toBe(false);
  });

  it('uses the user preferred mode when no local selection exists', async () => {
    await setup(
      { defaultMode: 'skill', allowModeToggle: true, skillsEnabled: true },
      { defaultModelId: null, preferredAgentMode: 'chat' },
    );
    expect(service.mode()).toBe('chat');
  });

  it('policy wins when toggling is disallowed', async () => {
    await setup(
      { defaultMode: 'skill', allowModeToggle: false, skillsEnabled: true },
      { defaultModelId: null, preferredAgentMode: 'chat' },
    );
    expect(service.mode()).toBe('skill');
    expect(service.canToggle()).toBe(false);

    // setMode is a no-op while locked
    service.setMode('chat', 'sess-1');
    expect(service.mode()).toBe('skill');
    expect(updateSettings).not.toHaveBeenCalled();
    expect(updateSessionPreferences).not.toHaveBeenCalled();
  });

  it('setMode flips the mode and persists user + session preferences', async () => {
    await setup({ defaultMode: 'skill', allowModeToggle: true, skillsEnabled: true });

    service.setMode('chat', 'sess-1');

    expect(service.mode()).toBe('chat');
    // silent: a failed background persist must not raise the error dialog
    expect(updateSettings).toHaveBeenCalledWith({ preferredAgentMode: 'chat' }, { silent: true });
    expect(updateSessionPreferences).toHaveBeenCalledWith('sess-1', { agentType: 'chat' });
  });

  it('setMode without a session persists only the user preference', async () => {
    await setup({ defaultMode: 'skill', allowModeToggle: true, skillsEnabled: true });

    service.setMode('chat');

    expect(service.mode()).toBe('chat');
    expect(updateSettings).toHaveBeenCalledWith({ preferredAgentMode: 'chat' }, { silent: true });
    expect(updateSessionPreferences).not.toHaveBeenCalled();
  });

  it('hydrateFromSession restores a stored mode and ignores missing ones', async () => {
    await setup({ defaultMode: 'skill', allowModeToggle: true, skillsEnabled: true });

    service.hydrateFromSession('chat');
    expect(service.mode()).toBe('chat');

    // A session without a stored mode keeps the current selection
    service.hydrateFromSession(undefined);
    expect(service.mode()).toBe('chat');

    service.hydrateFromSession('skill');
    expect(service.mode()).toBe('skill');
  });

  it('falls back to the dark default (skills off) when the policy endpoint fails', async () => {
    userSettings = undefined;
    updateSettings.mockClear();
    updateSessionPreferences.mockClear();

    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        ChatModeService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
        { provide: UserSettingsService, useValue: fakeUserSettingsService },
        { provide: SessionService, useValue: fakeSessionService },
      ],
    });

    service = TestBed.inject(ChatModeService);
    httpMock = TestBed.inject(HttpTestingController);

    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/system/chat-settings').error(new ProgressEvent('error'));
    });

    await vi.waitFor(() => {
      expect(service.policyLoaded()).toBe(true);
    });
    expect(service.mode()).toBe('chat');
    expect(service.canToggle()).toBe(false);
    expect(service.skillsEnabled()).toBe(false);
  });
});
