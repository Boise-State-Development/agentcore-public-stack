import { Component, ChangeDetectionStrategy, inject, signal, computed } from '@angular/core';
import { Router } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { DecimalPipe } from '@angular/common';
import { GeminiModelsService } from './services/gemini-models.service';
import { LoadingComponent } from '../../components/loading.component';
import { GeminiModelSummary } from './models/gemini-model.model';
import { ManagedModelsService } from '../manage-models/services/managed-models.service';

@Component({
  selector: 'app-gemini-models-page',
  imports: [FormsModule, LoadingComponent, DecimalPipe],
  templateUrl: './gemini-models.page.html',
  styleUrl: './gemini-models.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class GeminiModelsPage {
  private geminiModelsService = inject(GeminiModelsService);
  private managedModelsService = inject(ManagedModelsService);
  private router = inject(Router);

  // Filter signals
  maxResultsFilter = signal<number | undefined>(undefined);
  searchQuery = signal<string>('');

  // Access the models resource from the service
  readonly modelsResource = this.geminiModelsService.modelsResource;

  // Computed signal for models data
  readonly allModels = computed(() => this.modelsResource.value()?.models ?? []);

  // Computed signal for filtered models based on search query
  readonly models = computed(() => {
    const query = this.searchQuery().toLowerCase().trim();
    if (!query) {
      return this.allModels();
    }

    return this.allModels().filter(model =>
      model.name.toLowerCase().includes(query) ||
      model.displayName.toLowerCase().includes(query) ||
      model.description?.toLowerCase().includes(query)
    );
  });

  readonly totalCount = computed(() => this.modelsResource.value()?.totalCount ?? 0);
  readonly isLoading = computed(() => this.modelsResource.isLoading());
  readonly error = computed(() => {
    const err = this.modelsResource.error();
    return err ? String(err) : null;
  });

  /**
   * Apply the current filter values to the resource.
   * This triggers a refetch with the new parameters.
   */
  applyFilters(): void {
    this.geminiModelsService.updateModelsParams({
      maxResults: this.maxResultsFilter() || undefined,
    });

    // Explicitly reload the resource after updating params
    this.modelsResource.reload();
  }

  /**
   * Reset all filters and refetch data.
   */
  resetFilters(): void {
    this.maxResultsFilter.set(undefined);
    this.searchQuery.set('');
    this.geminiModelsService.resetModelsParams();
  }

  /**
   * Check if any filters are currently applied.
   */
  readonly hasActiveFilters = computed(() => {
    return !!(this.maxResultsFilter() || this.searchQuery());
  });

  /**
   * Check if a model has already been added to the managed models list
   */
  isModelAdded(modelName: string): boolean {
    return this.managedModelsService.isModelAdded(modelName);
  }

  /**
   * Check if a model supports streaming based on its generation methods
   *
   * Note: The Gemini API does not include 'streamGenerateContent' in the supportedGenerationMethods array.
   * However, if a model supports 'generateContent', it also supports streaming via the SDK's
   * generate_content_stream() method. This is a feature of how the SDK works, not explicitly listed in the API.
   */
  supportsStreaming(model: GeminiModelSummary): boolean {
    // If there are no methods listed, we can't determine streaming support
    if (!model.supportedGenerationMethods || model.supportedGenerationMethods.length === 0) {
      return false;
    }

    // If the model supports generateContent, it supports streaming
    // (even though streamGenerateContent is not listed in the array)
    const hasGenerateContent = model.supportedGenerationMethods.some(method =>
      method.toLowerCase() === 'generatecontent'
    );

    return hasGenerateContent;
  }

  /**
   * Navigate to add model form with prepopulated data from a Gemini model
   */
  addModelFromGemini(model: GeminiModelSummary): void {
    // Determine input/output modalities based on supported generation methods
    const hasGenerateContent = model.supportedGenerationMethods.some(
      method => method.toLowerCase().includes('generatecontent')
    );
    const inputModalities = hasGenerateContent ? ['TEXT'] : [];
    const outputModalities = hasGenerateContent ? ['TEXT'] : [];

    this.router.navigate(['/admin/manage-models/new'], {
      queryParams: {
        modelId: model.name,
        modelName: model.displayName,
        provider: 'gemini',
        providerName: 'Google',
        inputModalities: inputModalities.join(','),
        outputModalities: outputModalities.join(','),
        isReasoningModel: model.thinking || false,
        maxInputTokens: 1000000, // Default value for Gemini models, user can adjust
        maxOutputTokens: 8192, // Default value, user can adjust
      }
    });
  }
}
