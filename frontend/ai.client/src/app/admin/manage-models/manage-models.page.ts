import { Component, ChangeDetectionStrategy, signal, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { ManagedModelsService } from './services/managed-models.service';
import { AppRolesService } from '../roles/services/app-roles.service';

@Component({
  selector: 'app-manage-models-page',
  imports: [RouterLink, FormsModule],
  templateUrl: './manage-models.page.html',
  styleUrl: './manage-models.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ManageModelsPage {
  private managedModelsService = inject(ManagedModelsService);
  private appRolesService = inject(AppRolesService);

  // Search and filter signals
  searchQuery = signal<string>('');
  providerFilter = signal<string>('');
  enabledFilter = signal<string>('');

  // Get models from service
  private mockModels = computed(() => this.managedModelsService.getManagedModels());

  // Filtered models based on search and filters
  readonly filteredModels = computed(() => {
    let models = this.mockModels();
    const query = this.searchQuery().toLowerCase();
    const provider = this.providerFilter();
    const enabled = this.enabledFilter();

    if (query) {
      models = models.filter(
        m =>
          m.modelName.toLowerCase().includes(query) ||
          m.modelId.toLowerCase().includes(query) ||
          m.providerName.toLowerCase().includes(query)
      );
    }

    if (provider) {
      models = models.filter(m => m.providerName === provider);
    }

    if (enabled) {
      const isEnabled = enabled === 'enabled';
      models = models.filter(m => m.enabled === isEnabled);
    }

    return models;
  });

  // Available providers for filter dropdown
  readonly availableProviders = computed(() => {
    const providers = new Set(this.mockModels().map(m => m.providerName));
    return Array.from(providers).sort();
  });

  // Check if any filters are active
  readonly hasActiveFilters = computed(() => {
    return !!(this.searchQuery() || this.providerFilter() || this.enabledFilter());
  });

  /**
   * Reset all filters
   */
  resetFilters(): void {
    this.searchQuery.set('');
    this.providerFilter.set('');
    this.enabledFilter.set('');
  }

  /**
   * Delete a model
   */
  async deleteModel(modelId: string): Promise<void> {
    if (confirm('Are you sure you want to delete this model?')) {
      try {
        await this.managedModelsService.deleteModel(modelId);
      } catch (error) {
        console.error('Error deleting model:', error);
        alert('Failed to delete model. Please try again.');
      }
    }
  }

  /**
   * Get the display name for a role ID.
   * Falls back to the role ID if not found.
   */
  getRoleDisplayName(roleId: string): string {
    const role = this.appRolesService.getRoleById(roleId);
    return role?.displayName ?? roleId;
  }
}
