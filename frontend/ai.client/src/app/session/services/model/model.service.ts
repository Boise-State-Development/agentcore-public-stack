import { Injectable, signal, computed, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { ManagedModel } from '../../../admin/manage-models/models/managed-model.model';
import { UserSettingsService } from '../../../services/user-settings.service';

interface ManagedModelsListResponse {
  models: ManagedModel[];
  totalCount: number;
}

@Injectable({
  providedIn: 'root'
})
export class ModelService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private userSettings = inject(UserSettingsService);
  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/models`);

  // Session storage key for persisting model selection
  private readonly SELECTED_MODEL_KEY = 'selectedModelId';
  // Session storage key for persisting per-model inference param overrides.
  // Keyed by modelId so switching models doesn't bleed values across.
  private readonly INFERENCE_OVERRIDES_KEY = 'inferenceParamOverrides';

  // Default model used when no models are available (matches backend default)
  private readonly DEFAULT_MODEL: ManagedModel = {
    id: 'system-default',
    modelId: 'us.anthropic.claude-haiku-4-5-20251001-v1:0',
    modelName: 'System Default',
    provider: 'bedrock',
    providerName: 'Anthropic',
    inputModalities: ['TEXT'],
    outputModalities: ['TEXT'],
    maxInputTokens: 200000,
    maxOutputTokens: 4096,
    allowedAppRoles: [],
    availableToRoles: [],
    enabled: true,
    inputPricePerMillionTokens: 0,
    outputPricePerMillionTokens: 0,
    knowledgeCutoffDate: null,
    supportsCaching: true,
    isDefault: false,
  };

  // Models fetched from API
  private readonly models = signal<ManagedModel[]>([]);
  private readonly isLoading = signal<boolean>(false);
  private readonly error = signal<string | null>(null);
  private readonly usingDefaultModel = signal<boolean>(false);

  // Selected model (defaults to first model when available, or system default)
  private readonly _selectedModel = signal<ManagedModel | null>(null);

  // Per-model canonical inference param overrides. Outer key = modelId, inner
  // key = canonical param name (temperature, top_p, thinking, ...). Sent on
  // each chat request as `inference_params`; backend layers them on top of
  // admin defaults and clamps to the model's bounds.
  private readonly _inferenceOverrides = signal<Record<string, Record<string, unknown>>>(
    this.loadOverridesFromStorage(),
  );

  // Public read-only signals
  readonly availableModels = this.models.asReadonly();
  readonly selectedModel = computed(() => {
    const selected = this._selectedModel();
    if (selected) return selected;
    // Fallback to first available model if none selected, or default model if no models
    const models = this.models();
    if (models.length > 0) {
      return models[0];
    }
    // No models available, return default model
    return this.DEFAULT_MODEL;
  });
  readonly modelsLoading = this.isLoading.asReadonly();
  readonly modelsError = this.error.asReadonly();

  /** Inference param overrides for the currently selected model. */
  readonly selectedModelOverrides = computed<Record<string, unknown>>(() => {
    const model = this.selectedModel();
    if (!model) return {};
    return this._inferenceOverrides()[model.modelId] ?? {};
  });

  constructor() {
    // Load models on initialization
    this.loadModels().catch(err => {
      console.error('Failed to load models on initialization:', err);
    });
  }

  /**
   * Loads models from the API endpoint
   * Filters models by user roles automatically via the /models endpoint
   */
  async loadModels(): Promise<void> {
    this.isLoading.set(true);
    this.error.set(null);

    try {
      // Ensure user is authenticated before making the request
      const response = await firstValueFrom(
        this.http.get<ManagedModelsListResponse>(
          this.baseUrl()
        )
      );

      // Filter to only enabled models
      const enabledModels = response.models.filter(model => model.enabled);

      // Preserve selected model if it still exists in the new list
      const currentSelected = this._selectedModel();
      const wasUsingDefault = this.usingDefaultModel();
      const selectedStillExists = currentSelected && 
        enabledModels.some(m => m.modelId === currentSelected.modelId);

      this.models.set(enabledModels);

      // Set selected model with priority:
      // 1. Keep current in-memory selection if it still exists
      // 2. Restore from sessionStorage if available and model exists
      // 3. Select the admin-configured default model (isDefault: true)
      // 4. Otherwise, select first model if available
      // 5. If no models available, use system default
      if (selectedStillExists && currentSelected && !wasUsingDefault) {
        // Find and set the matching model (in case other fields changed)
        const matchingModel = enabledModels.find(m => m.modelId === currentSelected.modelId);
        if (matchingModel) {
          this._selectedModel.set(matchingModel);
          this.usingDefaultModel.set(false);
        }
      } else if (enabledModels.length > 0) {
        // Try to restore from sessionStorage first
        const savedModelId = this.getSavedModelId();
        const savedModel = savedModelId ? enabledModels.find(m => m.modelId === savedModelId) : null;

        if (savedModel) {
          // Restore previously selected model from session
          this._selectedModel.set(savedModel);
          this.usingDefaultModel.set(false);
        } else {
          // Check the user's persisted default from settings API before
          // falling back to the admin-configured default. Settings live in
          // DynamoDB and survive across sessions / browsers, where session
          // storage above is tab-scoped only.
          const userDefaultModel = await this.findUserDefaultModel(enabledModels);
          if (userDefaultModel) {
            this._selectedModel.set(userDefaultModel);
            this.usingDefaultModel.set(false);
          } else {
            // Find admin-configured default model, or fall back to first available
            const defaultModel = enabledModels.find(m => m.isDefault);
            this._selectedModel.set(defaultModel || enabledModels[0]);
            this.usingDefaultModel.set(false);
          }
        }
      } else {
        // No models available, use system default
        this._selectedModel.set(this.DEFAULT_MODEL);
        this.usingDefaultModel.set(true);
      }

      this.isLoading.set(false);
    } catch (err: unknown) {
      console.error('Error loading models:', err);
      const errorMessage = err instanceof Error ? err.message : 'Failed to load models';
      this.error.set(errorMessage);
      this.isLoading.set(false);
      
      // Set empty array on error and use default model
      this.models.set([]);
      this._selectedModel.set(this.DEFAULT_MODEL);
      this.usingDefaultModel.set(true);
    }
  }

  /**
   * Sets the selected model and persists to sessionStorage
   */
  setSelectedModel(model: ManagedModel): void {
    this._selectedModel.set(model);
    // Update flag to track if we're using the default model
    this.usingDefaultModel.set(model.id === this.DEFAULT_MODEL.id);
    // Persist selection to sessionStorage
    this.saveModelId(model.modelId);
  }

  /**
   * Gets the currently selected model (for non-signal contexts)
   */
  getSelectedModel(): ManagedModel | null {
    return this._selectedModel();
  }

  /**
   * Checks if the currently selected model is the system default
   */
  isUsingDefaultModel(): boolean {
    const selected = this._selectedModel();
    return selected?.id === this.DEFAULT_MODEL.id || this.usingDefaultModel();
  }

  /**
   * Gets the default model object
   */
  getDefaultModel(): ManagedModel {
    return this.DEFAULT_MODEL;
  }

  /**
   * Sets the selected model by its modelId string.
   * Useful when loading session preferences where only the modelId is stored.
   * If the modelId is not found in available models, the selection is not changed.
   *
   * @param modelId - The modelId string to find and select
   * @returns true if the model was found and selected, false otherwise
   */
  setSelectedModelById(modelId: string): boolean {
    const models = this.models();
    const model = models.find(m => m.modelId === modelId);

    if (model) {
      this._selectedModel.set(model);
      this.usingDefaultModel.set(false);
      // Persist selection to sessionStorage
      this.saveModelId(model.modelId);
      return true;
    }

    return false;
  }

  /**
   * Saves the selected model ID to sessionStorage
   */
  private saveModelId(modelId: string): void {
    try {
      sessionStorage.setItem(this.SELECTED_MODEL_KEY, modelId);
    } catch (e) {
      // SessionStorage may be unavailable in some contexts (e.g., private browsing)
      console.warn('Could not save model selection to sessionStorage:', e);
    }
  }

  /**
   * Retrieves the saved model ID from sessionStorage
   */
  private getSavedModelId(): string | null {
    try {
      return sessionStorage.getItem(this.SELECTED_MODEL_KEY);
    } catch (e) {
      // SessionStorage may be unavailable in some contexts
      console.warn('Could not read model selection from sessionStorage:', e);
      return null;
    }
  }

  /**
   * Look up the user's persisted defaultModelId from the settings API and
   * resolve it to a model in the supplied enabled list. Returns null when
   * the user has no default set, the saved model is no longer available,
   * or the settings call fails. Failures are swallowed because the caller
   * has a hardcoded fallback (admin default, then first available).
   */
  private async findUserDefaultModel(enabledModels: ManagedModel[]): Promise<ManagedModel | null> {
    try {
      const settings = await this.userSettings.fetchSettings();
      const id = settings?.defaultModelId;
      if (!id) return null;
      return enabledModels.find(m => m.modelId === id) ?? null;
    } catch (e) {
      console.warn('Could not load user settings to apply default model:', e);
      return null;
    }
  }

  /**
   * Set a single inference param override on the currently selected model.
   * Pass `null` / `undefined` to clear the override and fall back to the
   * admin default. No-op if no model is selected.
   */
  setInferenceParamOverride(paramKey: string, value: unknown): void {
    const model = this.selectedModel();
    if (!model) return;
    const next = { ...this._inferenceOverrides() };
    const modelOverrides = { ...(next[model.modelId] ?? {}) };
    if (value === null || value === undefined || value === '') {
      delete modelOverrides[paramKey];
    } else {
      modelOverrides[paramKey] = value;
    }
    if (Object.keys(modelOverrides).length === 0) {
      delete next[model.modelId];
    } else {
      next[model.modelId] = modelOverrides;
    }
    this._inferenceOverrides.set(next);
    this.persistOverrides(next);
  }

  /** Clear all inference param overrides for the currently selected model. */
  clearInferenceParamOverrides(): void {
    const model = this.selectedModel();
    if (!model) return;
    const next = { ...this._inferenceOverrides() };
    if (model.modelId in next) {
      delete next[model.modelId];
      this._inferenceOverrides.set(next);
      this.persistOverrides(next);
    }
  }

  /** Snapshot getter for non-signal contexts (e.g. request builders). */
  getInferenceParamOverrides(): Record<string, unknown> {
    return this.selectedModelOverrides();
  }

  private loadOverridesFromStorage(): Record<string, Record<string, unknown>> {
    try {
      const raw = sessionStorage.getItem(this.INFERENCE_OVERRIDES_KEY);
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch (e) {
      console.warn('Could not read inference overrides from sessionStorage:', e);
      return {};
    }
  }

  private persistOverrides(value: Record<string, Record<string, unknown>>): void {
    try {
      sessionStorage.setItem(this.INFERENCE_OVERRIDES_KEY, JSON.stringify(value));
    } catch (e) {
      console.warn('Could not save inference overrides to sessionStorage:', e);
    }
  }
}
