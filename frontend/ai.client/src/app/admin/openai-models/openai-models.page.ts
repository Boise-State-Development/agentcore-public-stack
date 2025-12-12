import { Component, ChangeDetectionStrategy, inject, signal, computed } from '@angular/core';
import { Router } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { OpenAIModelsService } from './services/openai-models.service';
import { LoadingComponent } from '../../components/loading.component';
import { OpenAIModelSummary } from './models/openai-model.model';
import { ManagedModelsService } from '../manage-models/services/managed-models.service';

@Component({
  selector: 'app-openai-models-page',
  imports: [FormsModule, LoadingComponent],
  templateUrl: './openai-models.page.html',
  styleUrl: './openai-models.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class OpenAIModelsPage {
  private openaiModelsService = inject(OpenAIModelsService);
  private managedModelsService = inject(ManagedModelsService);
  private router = inject(Router);

  // Filter signals
  maxResultsFilter = signal<number | undefined>(undefined);

  // Access the models resource from the service
  readonly modelsResource = this.openaiModelsService.modelsResource;

  // Computed signal for models data
  readonly models = computed(() => this.modelsResource.value()?.models ?? []);
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
    this.openaiModelsService.updateModelsParams({
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
    this.openaiModelsService.resetModelsParams();
  }

  /**
   * Check if any filters are currently applied.
   */
  readonly hasActiveFilters = computed(() => {
    return !!(this.maxResultsFilter());
  });

  /**
   * Check if a model has already been added to the managed models list
   */
  isModelAdded(modelId: string): boolean {
    return this.managedModelsService.isModelAdded(modelId);
  }

  /**
   * Format Unix timestamp to readable date
   */
  formatDate(timestamp?: number): string {
    if (!timestamp) return 'Unknown';
    return new Date(timestamp * 1000).toLocaleDateString();
  }

  /**
   * Navigate to add model form with prepopulated data from an OpenAI model
   */
  addModelFromOpenAI(model: OpenAIModelSummary): void {
    // Determine if it's a chat model (most OpenAI models support text input/output)
    const isChatModel = model.id.includes('gpt') || model.id.includes('o1') || model.id.includes('o3');
    const inputModalities = isChatModel ? ['TEXT'] : [];
    const outputModalities = isChatModel ? ['TEXT'] : [];

    // Determine default token limits based on model
    let maxInputTokens = 8192;
    let maxOutputTokens = 4096;

    if (model.id.includes('gpt-4o')) {
      maxInputTokens = 128000;
      maxOutputTokens = 16384;
    } else if (model.id.includes('gpt-4-turbo')) {
      maxInputTokens = 128000;
      maxOutputTokens = 4096;
    } else if (model.id.includes('gpt-4')) {
      maxInputTokens = 8192;
      maxOutputTokens = 4096;
    } else if (model.id.includes('gpt-3.5-turbo')) {
      maxInputTokens = 16385;
      maxOutputTokens = 4096;
    } else if (model.id.includes('o1')) {
      maxInputTokens = 200000;
      maxOutputTokens = 100000;
    }

    this.router.navigate(['/admin/manage-models/new'], {
      queryParams: {
        modelId: model.id,
        modelName: model.id, // OpenAI uses same ID as display name
        provider: 'openai',
        providerName: 'OpenAI',
        inputModalities: inputModalities.join(','),
        outputModalities: outputModalities.join(','),
        responseStreamingSupported: true, // Most OpenAI models support streaming
        maxInputTokens: maxInputTokens,
        maxOutputTokens: maxOutputTokens,
        modelLifecycle: 'ACTIVE',
      }
    });
  }
}
