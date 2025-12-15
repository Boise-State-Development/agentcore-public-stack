import { Injectable, inject, signal, resource } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../../environments/environment';
import { AuthService } from '../../../auth/auth.service';
import {
  BedrockModelsResponse,
  ListBedrockModelsParams
} from '../models/bedrock-model.model';

/**
 * Service for managing Bedrock foundation models via the admin API.
 *
 * Uses Angular's resource API for reactive data fetching with automatic
 * refetch when filter parameters change.
 */
@Injectable({
  providedIn: 'root'
})
export class BedrockModelsService {
  private http = inject(HttpClient);
  private authService = inject(AuthService);

  /**
   * Signal for filter parameters used by the models resource.
   * Update this signal to trigger a refetch with new filters.
   * Angular's resource API automatically tracks signals read within the loader.
   */
  private modelsParams = signal<ListBedrockModelsParams>({});

  /**
   * Reactive resource for fetching Bedrock models.
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
   * const response = bedrockModelsService.modelsResource.value();
   * const models = response?.models;
   *
   * // Check loading state
   * const isLoading = bedrockModelsService.modelsResource.isPending();
   *
   * // Handle errors
   * const error = bedrockModelsService.modelsResource.error();
   *
   * // Update filters to trigger refetch
   * bedrockModelsService.updateModelsParams({ byProvider: 'Anthropic' });
   *
   * // Manually refetch
   * bedrockModelsService.modelsResource.reload();
   * ```
   */
  readonly modelsResource = resource({
    loader: async () => {
      // Read params signal to make resource reactive to filter changes
      const params = this.modelsParams();

      // Ensure user is authenticated before making the request
      await this.authService.ensureAuthenticated();

      // Fetch models from API
      return this.getBedrockModels(params);
    }
  });

  /**
   * Updates the filter parameters for the models resource.
   * This will automatically trigger a refetch of the resource.
   *
   * @param params - New filter parameters
   */
  updateModelsParams(params: Partial<ListBedrockModelsParams>): void {
    this.modelsParams.update(current => {
      // Remove undefined values to create a clean params object
      const cleanParams: Partial<ListBedrockModelsParams> = {};

      if (params.byProvider !== undefined) cleanParams.byProvider = params.byProvider;
      if (params.byOutputModality !== undefined) cleanParams.byOutputModality = params.byOutputModality;
      if (params.byInferenceType !== undefined) cleanParams.byInferenceType = params.byInferenceType;
      if (params.byCustomizationType !== undefined) cleanParams.byCustomizationType = params.byCustomizationType;
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
   * Fetches Bedrock foundation models from the admin API.
   *
   * @param params - Optional filter parameters
   * @returns Promise resolving to BedrockModelsResponse
   * @throws Error if the API request fails or user lacks admin privileges
   *
   * @example
   * ```typescript
   * // Get all models
   * const response = await bedrockModelsService.getBedrockModels();
   *
   * // Get models filtered by provider
   * const anthropicModels = await bedrockModelsService.getBedrockModels({
   *   byProvider: 'Anthropic'
   * });
   *
   * // Get text output models with client-side limit
   * const textModels = await bedrockModelsService.getBedrockModels({
   *   byOutputModality: 'TEXT',
   *   maxResults: 50
   * });
   * ```
   */
  async getBedrockModels(params?: ListBedrockModelsParams): Promise<BedrockModelsResponse> {
    let httpParams = new HttpParams();

    if (params?.byProvider) {
      httpParams = httpParams.set('by_provider', params.byProvider);
    }

    if (params?.byOutputModality) {
      httpParams = httpParams.set('by_output_modality', params.byOutputModality);
    }

    if (params?.byInferenceType) {
      httpParams = httpParams.set('by_inference_type', params.byInferenceType);
    }

    if (params?.byCustomizationType) {
      httpParams = httpParams.set('by_customization_type', params.byCustomizationType);
    }

    if (params?.maxResults !== undefined) {
      httpParams = httpParams.set('max_results', params.maxResults.toString());
    }

    try {
      const response = await firstValueFrom(
        this.http.get<BedrockModelsResponse>(
          `${environment.appApiUrl}/admin/bedrock/models`,
          { params: httpParams }
        )
      );

      return response;
    } catch (error) {
      throw error;
    }
  }
}
