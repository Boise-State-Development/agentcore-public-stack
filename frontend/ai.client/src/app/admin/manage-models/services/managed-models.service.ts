import { Injectable, inject, computed, resource } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { ManagedModel, ManagedModelFormData } from '../models/managed-model.model';

/**
 * Response model for managed models list endpoint
 */
export interface ManagedModelsListResponse {
  models: ManagedModel[];
  totalCount: number;
}

/**
 * The result of uploading or clearing a model's icon.
 *
 * Both fields are absent after a remove, which is the signal the form needs to
 * fall back to the model's `iconSlug` without re-reading the catalog.
 */
export interface ManagedModelIconResponse {
  id: string;
  iconKey?: string | null;
  iconUrl?: string | null;
}

/**
 * A model on Bedrock Mantle's live roster (`GET /admin/mantle/models`).
 * Mirrors the OpenAI list-models shape the Mantle endpoint speaks.
 */
export interface MantleModelSummary {
  id: string;
  created?: number | null;
  ownedBy: string;
  object?: string | null;
}

/**
 * Response model for the Bedrock Mantle browse endpoint.
 */
export interface MantleModelsResponse {
  models: MantleModelSummary[];
  region: string;
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
  private config = inject(ConfigService);
  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/admin/managed-models`);

  /**
   * Reactive resource for fetching managed models.
   *
   * This resource automatically refetches when manually reloaded.
   * Provides reactive signals for data, loading state, and errors.
   */
  readonly modelsResource = resource({
    loader: async () => {
      await Promise.resolve();
      // Ensure user is authenticated before making the request
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
          this.baseUrl()
        )
      );

      return response;
    } catch (error) {
      throw error;
    }
  }

  /**
   * Fetch the live Bedrock Mantle roster for the deployment's region.
   *
   * Unlike the curated catalog, Mantle's available models are discovered
   * server-side (the backend authenticates against the regional Mantle
   * endpoint with an IAM-derived bearer token).
   *
   * @returns Promise resolving to MantleModelsResponse
   * @throws Error if the API request fails or user lacks admin privileges
   */
  async fetchMantleModels(): Promise<MantleModelsResponse> {
    return firstValueFrom(
      this.http.get<MantleModelsResponse>(
        `${this.config.appApiUrl()}/admin/mantle/models`
      )
    );
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
          this.baseUrl(),
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
          `${this.baseUrl()}/${modelId}`
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
          `${this.baseUrl()}/${modelId}`,
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
   * Upload a custom icon for a model.
   *
   * Multipart, not JSON: the bytes go straight to S3 and only the key lands on
   * the record. The server validates and re-encodes to 512×512 — which is also
   * what strips EXIF — so a rejection here carries an admin-facing reason and
   * should be surfaced verbatim rather than replaced with "upload failed".
   *
   * @param modelId - Model record identifier (the UUID, not the Bedrock model id)
   * @param file - A square PNG or JPEG, at least 256×256 and at most 400 KB
   * @returns Promise resolving to the stored key and the path that serves it
   */
  async uploadIcon(modelId: string, file: File): Promise<ManagedModelIconResponse> {
    const body = new FormData();
    body.append('file', file);

    const response = await firstValueFrom(
      this.http.post<ManagedModelIconResponse>(`${this.baseUrl()}/${modelId}/icon`, body),
    );

    // The catalog's cached copy still carries the old iconUrl (or none).
    this.modelsResource.reload();

    return response;
  }

  /**
   * Remove a model's uploaded icon, falling back to its `iconSlug`.
   *
   * Distinct from clearing `iconSlug` through the form: the two are independent,
   * and an admin who uploaded the wrong file should get their built-in logo back
   * rather than a blank tile.
   *
   * @param modelId - Model record identifier
   * @returns Promise resolving to the cleared icon fields
   */
  async deleteIcon(modelId: string): Promise<ManagedModelIconResponse> {
    const response = await firstValueFrom(
      this.http.delete<ManagedModelIconResponse>(`${this.baseUrl()}/${modelId}/icon`),
    );

    this.modelsResource.reload();

    return response;
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
          `${this.baseUrl()}/${modelId}`
        )
      );

      // Reload the resource to refresh the list
      this.modelsResource.reload();
    } catch (error) {
      throw error;
    }
  }
}
