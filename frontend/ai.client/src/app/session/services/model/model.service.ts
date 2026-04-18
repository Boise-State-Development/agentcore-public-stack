import { Injectable, signal, computed, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { AuthService } from '../../../auth/auth.service';
import { UserSettingsService } from '../../../services/user-settings.service';
import { ManagedModel } from '../../../admin/manage-models/models/managed-model.model';

interface ManagedModelsListResponse {
  models: ManagedModel[];
  totalCount: number;
}

@Injectable({
  providedIn: 'root'
})
export class ModelService {
  private http = inject(HttpClient);
  private authService = inject(AuthService);
  private config = inject(ConfigService);
  private userSettingsService = inject(UserSettingsService);
  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/models`);

  // Session storage key for persisting model selection
  private readonly SELECTED_MODEL_KEY = 'selectedModelId';

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
    isReasoningModel: false,
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
      await this.authService.ensureAuthenticated();

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
          // Set provisional selection synchronously so the UI has a value immediately.
          // Priority: admin-configured default (isDefault: true), else first available.
          const adminDefault = enabledModels.find(m => m.isDefault);
          this._selectedModel.set(adminDefault || enabledModels[0]);
          this.usingDefaultModel.set(false);

          // Then asynchronously upgrade to the user's persisted default if one is set.
          // We don't await inside the synchronous selection path so callers (and tests)
          // see a valid selection without waiting on the settings HTTP call.
          this.applyUserDefaultModel(enabledModels);
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
   * Asynchronously applies the user's persisted default model (from the settings API)
   * on top of an already-set provisional selection. Runs as a fire-and-forget task.
   * Only upgrades the selection if:
   *   - The user has a defaultModelId set
   *   - That model exists in the supplied enabled list
   *   - The user hasn't already made a different explicit selection while loading
   */
  private async applyUserDefaultModel(enabledModels: ManagedModel[]): Promise<void> {
    const selectionBeforeFetch = this._selectedModel();
    try {
      const settings = await this.userSettingsService.fetchSettings();
      const userDefaultId = settings?.defaultModelId;
      if (!userDefaultId) return;

      // Skip if the user changed the selection while we were fetching.
      if (this._selectedModel() !== selectionBeforeFetch) return;

      const userDefault = enabledModels.find(m => m.modelId === userDefaultId);
      if (userDefault) {
        this._selectedModel.set(userDefault);
        this.usingDefaultModel.set(false);
      }
    } catch (e) {
      console.warn('Could not load user default model from settings:', e);
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
}
