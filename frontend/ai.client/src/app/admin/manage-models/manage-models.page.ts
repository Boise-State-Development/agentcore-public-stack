import {
  Component,
  ChangeDetectionStrategy,
  Injector,
  afterNextRender,
  signal,
  computed,
  inject,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import {
  CdkDrag,
  CdkDragDrop,
  CdkDragHandle,
  CdkDropList,
  moveItemInArray,
} from '@angular/cdk/drag-drop';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroPlus,
  heroMagnifyingGlass,
  heroPencilSquare,
  heroBars2,
} from '@ng-icons/heroicons/outline';
import { heroStarSolid } from '@ng-icons/heroicons/solid';
import { ManagedModelsService } from './services/managed-models.service';
import type { ManagedModel } from './models/managed-model.model';
import { SpinnerComponent } from '../../components/spinner/spinner.component';
import { ModelIconComponent } from '../../components/model-icon/model-icon.component';
import { TooltipDirective } from '../../components/tooltip/tooltip.directive';

@Component({
  selector: 'app-manage-models-page',
  imports: [
    RouterLink,
    FormsModule,
    NgIcon,
    ModelIconComponent,
    SpinnerComponent,
    TooltipDirective,
    CdkDropList,
    CdkDrag,
    CdkDragHandle,
  ],
  providers: [
    provideIcons({
      heroPlus,
      heroMagnifyingGlass,
      heroPencilSquare,
      heroBars2,
      heroStarSolid,
    }),
  ],
  templateUrl: './manage-models.page.html',
  styleUrl: './manage-models.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ManageModelsPage {
  protected managedModelsService = inject(ManagedModelsService);
  private injector = inject(Injector);

  // Search and filter signals
  searchQuery = signal<string>('');
  providerFilter = signal<string>('');
  enabledFilter = signal<string>('');

  // Models with an in-flight enable/disable request
  private togglingIds = signal<ReadonlySet<string>>(new Set());

  private allModels = computed(() => this.managedModelsService.getManagedModels());

  // Resource state for the page's loading / error overlays.
  protected readonly modelsResource = this.managedModelsService.modelsResource;
  protected readonly isInitialLoad = computed(
    () => this.modelsResource.isLoading() && this.allModels().length === 0,
  );

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
   * Reordering is only offered on the unfiltered list. Dropping a row between
   * two rows of a filtered subset has no single meaning in the full catalog —
   * the hidden models in between would have to go somewhere the admin can't see.
   */
  readonly canReorder = computed(() => !this.hasActiveFilters() && this.allModels().length > 1);

  /** True while an order is being saved; drives the "Saving order…" hint. */
  protected readonly savingOrder = signal(false);

  /** Screen-reader announcement for the last move (a polite live region). */
  protected readonly reorderAnnouncement = signal('');

  // Only the latest move reports a failure: every overlapping move awaits the
  // same coalesced save, and one failure shouldn't raise one alert per keypress.
  private moveSeq = 0;

  onDrop(event: CdkDragDrop<ManagedModel[]>): void {
    this.moveModel(event.previousIndex, event.currentIndex);
  }

  /**
   * Keyboard reordering on the drag handle — CDK drag and drop is pointer-only.
   * Arrow keys move one place, Home/End to either end.
   */
  onHandleKeydown(event: KeyboardEvent, index: number): void {
    const last = this.allModels().length - 1;
    const target =
      event.key === 'ArrowUp' ? index - 1
      : event.key === 'ArrowDown' ? index + 1
      : event.key === 'Home' ? 0
      : event.key === 'End' ? last
      : null;
    if (target === null) {
      return;
    }
    event.preventDefault();
    const modelId = this.allModels()[index]?.id;
    this.moveModel(index, Math.max(0, Math.min(last, target)));

    // The moved row's DOM node may be detached and re-inserted, which drops
    // focus; put it back so the admin can keep pressing the arrow key.
    afterNextRender(
      () => document.getElementById(`reorder-handle-${modelId}`)?.focus(),
      { injector: this.injector },
    );
  }

  private async moveModel(from: number, to: number): Promise<void> {
    if (from === to || !this.canReorder()) {
      return;
    }
    const models = [...this.allModels()];
    const moved = models[from];
    moveItemInArray(models, from, to);
    this.reorderAnnouncement.set(
      `${moved.modelName} moved to position ${to + 1} of ${models.length}.`,
    );

    const seq = ++this.moveSeq;
    this.savingOrder.set(true);
    try {
      await this.managedModelsService.reorderModels(models.map(m => m.id));
    } catch (error) {
      console.error('Error saving model order:', error);
      if (seq === this.moveSeq) {
        alert('Failed to save the model order. The list has been reloaded — please try again.');
      }
    } finally {
      if (seq === this.moveSeq) {
        this.savingOrder.set(false);
      }
    }
  }

  /**
   * Reset all filters
   */
  resetFilters(): void {
    this.searchQuery.set('');
    this.providerFilter.set('');
    this.enabledFilter.set('');
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
}
