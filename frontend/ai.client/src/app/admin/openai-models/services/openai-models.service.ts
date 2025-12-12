import { Injectable, inject, signal, resource } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../../environments/environment';
import { AuthService } from '../../../auth/auth.service';
import {
  OpenAIModelsResponse,
  ListOpenAIModelsParams
} from '../models/openai-model.model';

/**
 * Service for managing OpenAI models via the admin API.
 *
 * Uses Angular's resource API for reactive data fetching with automatic
 * refetch when filter parameters change.
 */
@Injectable({
  providedIn: 'root'
})
export class OpenAIModelsService {
  private http = inject(HttpClient);
  private authService = inject(AuthService);

  /**
   * Signal for filter parameters used by the models resource.
   * Update this signal to trigger a refetch with new filters.
   */
  private modelsParams = signal<ListOpenAIModelsParams>({});

  /**
   * Reactive resource for fetching OpenAI models.
   *
   * This resource automatically refetches when `modelsParams` signal changes
   * because Angular's resource API tracks signals read within the loader function.
   * Provides reactive signals for data, loading state, and errors.
   *
   * The resource ensures the user is authenticated before making the HTTP request.
   *
   * @example
   * ```typescript
   * // Access data (may be undefined initially)
   * const response = openaiModelsService.modelsResource.value();
   * const models = response?.models;
   *
   * // Check loading state
   * const isLoading = openaiModelsService.modelsResource.isLoading();
   *
   * // Handle errors
   * const error = openaiModelsService.modelsResource.error();
   *
   * // Manually refetch
   * openaiModelsService.modelsResource.reload();
   * ```
   */
  readonly modelsResource = resource({
    loader: async () => {
      // Read params signal to make resource reactive to filter changes
      const params = this.modelsParams();

      // Ensure user is authenticated before making the request
      await this.authService.ensureAuthenticated();

      // Fetch models from API
      return this.getOpenAIModels(params);
    }
  });

  /**
   * Updates the filter parameters for the models resource.
   * This will automatically trigger a refetch of the resource.
   *
   * @param params - New filter parameters
   */
  updateModelsParams(params: Partial<ListOpenAIModelsParams>): void {
    this.modelsParams.update(current => {
      // Remove undefined values to create a clean params object
      const cleanParams: Partial<ListOpenAIModelsParams> = {};

      if (params.maxResults !== undefined) cleanParams.maxResults = params.maxResults;

      return { ...cleanParams };
    });
  }

  /**
   * Resets all filter parameters to default values and triggers a refetch.
   */
  resetModelsParams(): void {
    this.modelsParams.set({});
  }

  /**
   * Fetches OpenAI models from the admin API.
   *
   * @param params - Optional filter parameters
   * @returns Promise resolving to OpenAIModelsResponse
   * @throws Error if the API request fails or user lacks admin privileges
   *
   * @example
   * ```typescript
   * // Get all models
   * const response = await openaiModelsService.getOpenAIModels();
   *
   * // Get models with client-side limit
   * const limitedModels = await openaiModelsService.getOpenAIModels({
   *   maxResults: 20
   * });
   * ```
   */
  async getOpenAIModels(params?: ListOpenAIModelsParams): Promise<OpenAIModelsResponse> {
    let httpParams = new HttpParams();

    if (params?.maxResults !== undefined) {
      httpParams = httpParams.set('max_results', params.maxResults.toString());
    }

    try {
      const response = await firstValueFrom(
        this.http.get<OpenAIModelsResponse>(
          `${environment.appApiUrl}/admin/openai/models`,
          { params: httpParams }
        )
      );

      return response;
    } catch (error) {
      throw error;
    }
  }
}
