import { Component, ChangeDetectionStrategy, inject, signal, computed } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroPlus,
  heroMagnifyingGlass,
  heroChevronDown,
} from '@ng-icons/heroicons/outline';
import { heroCheckCircleSolid } from '@ng-icons/heroicons/solid';
import { OpenAIModelsService } from './services/openai-models.service';
import { OpenAIModelSummary } from './models/openai-model.model';
import { ManagedModelsService } from '../manage-models/services/managed-models.service';
import { ThinkingDotsComponent } from '../../components/thinking-dots.component';

@Component({
  selector: 'app-openai-models-page',
  imports: [FormsModule, ThinkingDotsComponent, RouterLink, NgIcon],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroPlus,
      heroMagnifyingGlass,
      heroChevronDown,
      heroCheckCircleSolid,
    }),
  ],
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
  searchQuery = signal<string>('');

  // Row detail expansion state (set of model ids currently expanded)
  private expandedIds = signal<ReadonlySet<string>>(new Set());

  // Access the models resource from the service
  readonly modelsResource = this.openaiModelsService.modelsResource;

  // Computed signal for models data
  readonly allModels = computed(() => this.modelsResource.value()?.models ?? []);

  // Computed signal for filtered models based on search query
  readonly models = computed(() => {
    const query = this.searchQuery().toLowerCase().trim();
    if (!query) {
      return this.allModels();
    }

    return this.allModels().filter(model =>
      model.id.toLowerCase().includes(query) ||
      model.ownedBy?.toLowerCase().includes(query)
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
    this.searchQuery.set('');
    this.openaiModelsService.resetModelsParams();
  }

  /**
   * Check if any filters are currently applied.
   */
  readonly hasActiveFilters = computed(() => {
    return !!(this.maxResultsFilter() || this.searchQuery());
  });

  isExpanded(modelId: string): boolean {
    return this.expandedIds().has(modelId);
  }

  toggleExpand(modelId: string): void {
    this.expandedIds.update(current => {
      const next = new Set(current);
      if (next.has(modelId)) {
        next.delete(modelId);
      } else {
        next.add(modelId);
      }
      return next;
    });
  }

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
        maxInputTokens: maxInputTokens,
        maxOutputTokens: maxOutputTokens,
      }
    });
  }
}
