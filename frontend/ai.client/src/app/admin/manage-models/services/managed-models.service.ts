import { Injectable, inject, signal, computed, resource } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../../environments/environment';
import { AuthService } from '../../../auth/auth.service';
import { ManagedModel, ManagedModelFormData } from '../models/managed-model.model';

/**
 * Response model for managed models list endpoint
 */
export interface ManagedModelsListResponse {
  models: ManagedModel[];
  totalCount: number;
}

/**
 * Service to manage the list of models that have been added to the system.
 * This service maintains the state of managed models and provides utilities
 * to check if a model has already been added.
 */
@Injectable({
  providedIn: 'root'
})
export class ManagedModelsService {
  private http = inject(HttpClient);
  private authService = inject(AuthService);

  /**
   * Reactive resource for fetching managed models.
   *
   * This resource automatically refetches when manually reloaded.
   * Provides reactive signals for data, loading state, and errors.
   */
  readonly modelsResource = resource({
    loader: async () => {
      // Ensure user is authenticated before making the request
      await this.authService.ensureAuthenticated();

      // Fetch models from API
      return this.fetchManagedModels();
    }
  });

  // Computed set of model IDs for quick lookup
  readonly addedModelIds = computed(() => {
    const models = this.modelsResource.value()?.models ?? [];
    return new Set(models.map(m => m.modelId));
  });

  /**
   * Get all managed models (from resource)
   */
  getManagedModels(): ManagedModel[] {
    return this.modelsResource.value()?.models ?? [];
  }

  /**
   * Check if a model with the given modelId has already been added
   */
  isModelAdded(modelId: string): boolean {
    return this.addedModelIds().has(modelId);
  }

  /**
   * Fetches managed models from the admin API.
   *
   * @returns Promise resolving to ManagedModelsListResponse
   * @throws Error if the API request fails or user lacks admin privileges
   */
  async fetchManagedModels(): Promise<ManagedModelsListResponse> {
    try {
      const response = await firstValueFrom(
        this.http.get<ManagedModelsListResponse>(
          `${environment.appApiUrl}/admin/managed-models`
        )
      );

      return response;
    } catch (error) {
      throw error;
    }
  }

  /**
   * Create a new managed model
   *
   * @param modelData - Model creation data
   * @returns Promise resolving to the created model
   * @throws Error if the API request fails
   */
  async createModel(modelData: ManagedModelFormData): Promise<ManagedModel> {
    try {
      const response = await firstValueFrom(
        this.http.post<ManagedModel>(
          `${environment.appApiUrl}/admin/managed-models`,
          modelData
        )
      );

      // Reload the resource to refresh the list
      this.modelsResource.reload();

      return response;
    } catch (error) {
      throw error;
    }
  }

  /**
   * Get a specific enabled model by ID
   *
   * @param modelId - Model identifier
   * @returns Promise resolving to the model
   * @throws Error if the API request fails or model not found
   */
  async getModel(modelId: string): Promise<ManagedModel> {
    try {
      const response = await firstValueFrom(
        this.http.get<ManagedModel>(
          `${environment.appApiUrl}/admin/managed-models/${modelId}`
        )
      );

      return response;
    } catch (error) {
      throw error;
    }
  }

  /**
   * Update an enabled model
   *
   * @param modelId - Model identifier
   * @param updates - Fields to update
   * @returns Promise resolving to the updated model
   * @throws Error if the API request fails or model not found
   */
  async updateModel(modelId: string, updates: Partial<ManagedModelFormData>): Promise<ManagedModel> {
    try {
      const response = await firstValueFrom(
        this.http.put<ManagedModel>(
          `${environment.appApiUrl}/admin/managed-models/${modelId}`,
          updates
        )
      );

      // Reload the resource to refresh the list
      this.modelsResource.reload();

      return response;
    } catch (error) {
      throw error;
    }
  }

  /**
   * Delete an enabled model
   *
   * @param modelId - Model identifier
   * @returns Promise resolving when deletion completes
   * @throws Error if the API request fails or model not found
   */
  async deleteModel(modelId: string): Promise<void> {
    try {
      await firstValueFrom(
        this.http.delete<void>(
          `${environment.appApiUrl}/admin/managed-models/${modelId}`
        )
      );

      // Reload the resource to refresh the list
      this.modelsResource.reload();
    } catch (error) {
      throw error;
    }
  }
}
