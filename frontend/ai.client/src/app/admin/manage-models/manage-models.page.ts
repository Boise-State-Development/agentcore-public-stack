import { Component, ChangeDetectionStrategy, signal, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroPlus,
  heroMagnifyingGlass,
  heroChevronDown,
  heroPencilSquare,
  heroTrash,
} from '@ng-icons/heroicons/outline';
import { heroStarSolid } from '@ng-icons/heroicons/solid';
import { ManagedModelsService } from './services/managed-models.service';
import { AppRolesService } from '../roles/services/app-roles.service';
import type { ManagedModel } from './models/managed-model.model';

@Component({
  selector: 'app-manage-models-page',
  imports: [RouterLink, FormsModule, NgIcon],
  providers: [
    provideIcons({
      heroPlus,
      heroMagnifyingGlass,
      heroChevronDown,
      heroPencilSquare,
      heroTrash,
      heroStarSolid,
    }),
  ],
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

  // Row detail expansion state (set of model ids currently expanded)
  private expandedIds = signal<ReadonlySet<string>>(new Set());

  // Models with an in-flight enable/disable request
  private togglingIds = signal<ReadonlySet<string>>(new Set());

  private allModels = computed(() => this.managedModelsService.getManagedModels());

  // Filtered models based on search and filters
  readonly filteredModels = computed(() => {
    let models = this.allModels();
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
    const providers = new Set(this.allModels().map(m => m.providerName));
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

  isToggling(modelId: string): boolean {
    return this.togglingIds().has(modelId);
  }

  /**
   * Flip a model's enabled state in place via a partial update.
   */
  async toggleEnabled(model: ManagedModel): Promise<void> {
    if (this.isToggling(model.id)) {
      return;
    }
    this.togglingIds.update(current => new Set(current).add(model.id));
    try {
      await this.managedModelsService.updateModel(model.id, { enabled: !model.enabled });
    } catch (error) {
      console.error('Error updating model status:', error);
      alert('Failed to update model status. Please try again.');
    } finally {
      this.togglingIds.update(current => {
        const next = new Set(current);
        next.delete(model.id);
        return next;
      });
    }
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
