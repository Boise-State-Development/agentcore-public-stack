import { Component, ChangeDetectionStrategy, inject, signal, computed } from '@angular/core';
import { Router } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { BedrockModelsService } from './services/bedrock-models.service';
import { FoundationModelSummary } from './models/bedrock-model.model';
import { ManagedModelsService } from '../manage-models/services/managed-models.service';
import { ThinkingDotsComponent } from '../../components/thinking-dots.component';

@Component({
  selector: 'app-bedrock-models-page',
  imports: [FormsModule, ThinkingDotsComponent],
  templateUrl: './bedrock-models.page.html',
  styleUrl: './bedrock-models.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class BedrockModelsPage {
  private bedrockModelsService = inject(BedrockModelsService);
  private managedModelsService = inject(ManagedModelsService);
  private router = inject(Router);

  // Filter signals
  providerFilter = signal<string>('');
  outputModalityFilter = signal<string>('');
  inferenceTypeFilter = signal<string>('');
  customizationTypeFilter = signal<string>('');
  maxResultsFilter = signal<number | undefined>(undefined);
  searchQuery = signal<string>('');

  // Access the models resource from the service
  readonly modelsResource = this.bedrockModelsService.modelsResource;

  // Computed signal for models data
  readonly allModels = computed(() => this.modelsResource.value()?.models ?? []);

  // Computed signal for filtered models based on search query
  readonly models = computed(() => {
    const query = this.searchQuery().toLowerCase().trim();
    if (!query) {
      return this.allModels();
    }

    return this.allModels().filter(model =>
      model.modelId.toLowerCase().includes(query) ||
      model.modelName.toLowerCase().includes(query) ||
      model.providerName.toLowerCase().includes(query)
    );
  });

  readonly totalCount = computed(() => this.modelsResource.value()?.totalCount ?? 0);
  readonly isLoading = computed(() => this.modelsResource.isLoading());
  readonly error = computed(() => {
    const err = this.modelsResource.error();
    return err ? String(err) : null;
  });

  // Available filter options (populated from data)
  readonly availableProviders = computed(() => {
    const models = this.allModels();
    const providers = new Set(models.map(m => m.providerName));
    return Array.from(providers).sort();
  });

  readonly availableOutputModalities = computed(() => {
    const models = this.allModels();
    const modalities = new Set(models.flatMap(m => m.outputModalities));
    return Array.from(modalities).sort();
  });

  readonly availableInferenceTypes = computed(() => {
    const models = this.allModels();
    const types = new Set(models.flatMap(m => m.inferenceTypesSupported));
    return Array.from(types).sort();
  });

  readonly availableCustomizationTypes = computed(() => {
    const models = this.allModels();
    const types = new Set(models.flatMap(m => m.customizationsSupported));
    return Array.from(types).sort();
  });

  /**
   * Apply the current filter values to the resource.
   * This triggers a refetch with the new parameters.
   */
  applyFilters(): void {
    this.bedrockModelsService.updateModelsParams({
      byProvider: this.providerFilter() || undefined,
      byOutputModality: this.outputModalityFilter() || undefined,
      byInferenceType: this.inferenceTypeFilter() || undefined,
      byCustomizationType: this.customizationTypeFilter() || undefined,
      maxResults: this.maxResultsFilter() || undefined,
    });

    // Explicitly reload the resource after updating params
    this.modelsResource.reload();
  }

  /**
   * Reset all filters and refetch data.
   */
  resetFilters(): void {
    this.providerFilter.set('');
    this.outputModalityFilter.set('');
    this.inferenceTypeFilter.set('');
    this.customizationTypeFilter.set('');
    this.maxResultsFilter.set(undefined);
    this.searchQuery.set('');
    this.bedrockModelsService.resetModelsParams();
  }

  /**
   * Check if any filters are currently applied.
   */
  readonly hasActiveFilters = computed(() => {
    return !!(
      this.providerFilter() ||
      this.outputModalityFilter() ||
      this.inferenceTypeFilter() ||
      this.customizationTypeFilter() ||
      this.maxResultsFilter() ||
      this.searchQuery()
    );
  });

  /**
   * Check if a model has already been added to the managed models list
   */
  isModelAdded(modelId: string): boolean {
    return this.managedModelsService.isModelAdded(modelId);
  }

  /**
   * Navigate to add model form with prepopulated data from a Bedrock model
   */
  addModelFromBedrock(model: FoundationModelSummary): void {
    this.router.navigate(['/admin/manage-models/new'], {
      queryParams: {
        modelId: model.modelId,
        modelName: model.modelName,
        provider: 'bedrock',
        providerName: model.providerName,
        inputModalities: model.inputModalities.join(','),
        outputModalities: model.outputModalities.join(','),
        maxInputTokens: 200000, // Default value, user can adjust
        maxOutputTokens: 4096, // Default value, user can adjust
      }
    });
  }
}
