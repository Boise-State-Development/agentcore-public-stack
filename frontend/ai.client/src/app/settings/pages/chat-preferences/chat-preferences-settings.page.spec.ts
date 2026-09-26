import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { provideRouter } from '@angular/router';
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

describe('ChatPreferencesSettingsPage — personal instructions', () => {
  let settingsValue: ReturnType<typeof signal<{ defaultModelId: string | null; personalInstructions?: string | null } | undefined>>;
  let updateSettings: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    TestBed.resetTestingModule();
    settingsValue = signal<{ defaultModelId: string | null; personalInstructions?: string | null } | undefined>(undefined);
    updateSettings = vi.fn();
    TestBed.configureTestingModule({
      imports: [ChatPreferencesSettingsPage],
      providers: [
        provideRouter([]),
        { provide: ModelService, useValue: { availableModels: signal([]), modelsLoading: signal(false), successorFor: () => null, modelNameFor: () => null } },
        {
          provide: UserSettingsService,
          useValue: { settingsResource: { value: () => settingsValue(), error: () => null, reload: vi.fn() }, updateSettings },
        },
        {
          provide: LocalSettingsService,
          useValue: { showTokenCount: signal(false), showDebugOutput: signal(false), setShowTokenCount: vi.fn(), setShowDebugOutput: vi.fn() },
        },
      ],
    });
  });

  function render() {
    const fixture = TestBed.createComponent(ChatPreferencesSettingsPage);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const textarea = () => el.querySelector<HTMLTextAreaElement>('#personal-instructions')!;
    const save = () => Array.from(el.querySelectorAll('button')).find(b => b.textContent?.trim().startsWith('Sav'))!;
    return { fixture, el, textarea, save };
  }

  function type(fixture: ReturnType<typeof render>['fixture'], textarea: HTMLTextAreaElement, value: string): void {
    textarea.value = value;
    textarea.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  it('is disabled until settings load, then shows the saved text and its length', () => {
    const { fixture, el, textarea } = render();
    expect(textarea().disabled).toBe(true);
    settingsValue.set({ defaultModelId: null, personalInstructions: 'Be brief.' });
    fixture.detectChanges();
    expect(textarea().disabled).toBe(false);
    expect(textarea().value).toBe('Be brief.');
    expect(el.textContent).toContain('9 / 4,000 characters');
  });

  it('saves only a real change, quietly, and says so', async () => {
    settingsValue.set({ defaultModelId: null, personalInstructions: 'Be brief.' });
    const { fixture, el, textarea, save } = render();
    expect(save().disabled).toBe(true);
    type(fixture, textarea(), 'Be brief.  ');
    expect(save().disabled).toBe(true);

    updateSettings.mockResolvedValue({ defaultModelId: null, personalInstructions: 'Use SI units.' });
    type(fixture, textarea(), 'Use SI units.');
    expect(save().disabled).toBe(false);
    save().click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(updateSettings).toHaveBeenCalledWith({ personalInstructions: 'Use SI units.' }, { silent: true });
    expect(el.querySelector('[role=status]')?.textContent).toContain('Saved');
  });

  it('clearing says Cleared', async () => {
    settingsValue.set({ defaultModelId: null, personalInstructions: 'Be brief.' });
    const { fixture, el, textarea, save } = render();
    updateSettings.mockResolvedValue({ defaultModelId: null, personalInstructions: null });
    type(fixture, textarea(), '');
    save().click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(el.querySelector('[role=status]')?.textContent).toContain('Cleared');
  });

  it('shows the API’s sentence when the save fails', async () => {
    settingsValue.set({ defaultModelId: null, personalInstructions: '' });
    const { fixture, el, textarea, save } = render();
    const { HttpErrorResponse } = await import('@angular/common/http');
    updateSettings.mockRejectedValue(new HttpErrorResponse({ status: 503, error: { detail: 'Settings storage is not configured.' } }));
    type(fixture, textarea(), 'Hi');
    save().click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(el.querySelector('[role=status]')?.textContent).toContain('Settings storage is not configured.');
  });
});
