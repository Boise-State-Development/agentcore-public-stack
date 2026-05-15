import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { ChatPreferencesSettingsPage } from './chat-preferences-settings.page';
import { ModelService } from '../../../session/services/model/model.service';
import { UserSettingsService } from '../../../services/user-settings.service';
import { LocalSettingsService } from '../../../services/local-settings.service';

/**
 * Regression tests for #161 — default model selection silently reverting on
 * page reload. The dropdown is bound via `[value]` on a native <select>,
 * which is a one-time DOM property write. If the saved `defaultModelId` is
 * emitted before the @for loop has rendered the matching <option>, the
 * browser silently resets the <select> to "" and Angular never re-applies
 * the binding when the options finally arrive (the computed input has not
 * changed).
 *
 * Fix: `currentDefaultModelId` returns '' until BOTH the user's settings
 * AND the model list have loaded. These tests pin that contract so a
 * future refactor can't quietly undo it.
 */
describe('ChatPreferencesSettingsPage — currentDefaultModelId', () => {
  let availableModels: ReturnType<typeof signal<{ id: string; modelId: string; modelName: string; providerName: string }[]>>;
  let settingsValue: ReturnType<typeof signal<{ defaultModelId: string | null } | undefined>>;
  let modelsLoading: ReturnType<typeof signal<boolean>>;

  beforeEach(() => {
    availableModels = signal<{ id: string; modelId: string; modelName: string; providerName: string }[]>([]);
    settingsValue = signal<{ defaultModelId: string | null } | undefined>(undefined);
    modelsLoading = signal<boolean>(false);

    const mockModelService = {
      availableModels,
      modelsLoading,
    };

    const mockUserSettingsService = {
      settingsResource: {
        value: () => settingsValue(),
        reload: vi.fn(),
      },
      updateSettings: vi.fn(),
    };

    const mockLocalSettings = {
      showTokenCount: signal(false),
      showDebugOutput: signal(false),
      setShowTokenCount: vi.fn(),
      setShowDebugOutput: vi.fn(),
    };

    TestBed.configureTestingModule({
      providers: [
        ChatPreferencesSettingsPage,
        { provide: ModelService, useValue: mockModelService },
        { provide: UserSettingsService, useValue: mockUserSettingsService },
        { provide: LocalSettingsService, useValue: mockLocalSettings },
      ],
    });
  });

  it("returns '' while neither data source has loaded", () => {
    const page = TestBed.inject(ChatPreferencesSettingsPage);
    expect(page.currentDefaultModelId()).toBe('');
  });

  it("returns '' when settings have loaded but the model list is still empty", () => {
    // This is the exact race the bug describes: settings resolve first,
    // model list is still empty, so binding the saved id at this moment
    // would target an <option> that does not yet exist.
    settingsValue.set({ defaultModelId: 'claude-haiku' });
    const page = TestBed.inject(ChatPreferencesSettingsPage);
    expect(page.currentDefaultModelId()).toBe('');
  });

  it("returns '' when the model list has loaded but settings are still pending", () => {
    availableModels.set([
      { id: '1', modelId: 'claude-haiku', modelName: 'Claude Haiku', providerName: 'Anthropic' },
    ]);
    const page = TestBed.inject(ChatPreferencesSettingsPage);
    expect(page.currentDefaultModelId()).toBe('');
  });

  it('returns the saved modelId once both data sources have loaded', () => {
    settingsValue.set({ defaultModelId: 'claude-haiku' });
    availableModels.set([
      { id: '1', modelId: 'claude-haiku', modelName: 'Claude Haiku', providerName: 'Anthropic' },
    ]);
    const page = TestBed.inject(ChatPreferencesSettingsPage);
    expect(page.currentDefaultModelId()).toBe('claude-haiku');
  });

  it("returns '' when the user has explicitly cleared their default", () => {
    settingsValue.set({ defaultModelId: null });
    availableModels.set([
      { id: '1', modelId: 'claude-haiku', modelName: 'Claude Haiku', providerName: 'Anthropic' },
    ]);
    const page = TestBed.inject(ChatPreferencesSettingsPage);
    expect(page.currentDefaultModelId()).toBe('');
  });
});
