import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { SpinnerComponent } from '../../components/spinner/spinner.component';
import { ModelsTabsComponent } from './components/models-tabs.component';
import { TaskModelCardComponent } from './components/task-model-card.component';
import { TaskModelsService } from './services/task-models.service';

/**
 * The Task models tab: which catalog model (and effort) the platform's own tasks run on.
 * docs/specs/admin-model-assignments.md.
 */
@Component({
  selector: 'app-task-models-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, SpinnerComponent, ModelsTabsComponent, TaskModelCardComponent],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8">
        <div class="mb-6">
          <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">Manage Models</h1>
          <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
            Choose the models the platform uses for its own background tasks. Each task keeps a
            built-in default, which runs until you assign a model and again if the assigned model is
            turned off or removed. Only enabled Bedrock models in the
            <a routerLink="/admin/manage-models" class="font-medium text-primary-accessible hover:underline dark:text-primary-accessible-dark">catalog</a>
            can be assigned; users don't need access to them.
          </p>
        </div>

        <app-models-tabs />

        @if (isInitialLoad()) {
          <div class="flex h-64 items-center justify-center rounded-2xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
            <app-spinner size="xl" label="Loading task models" />
          </div>
        } @else if (slotsResource.error()) {
          <div class="rounded-2xl border border-state-danger-200 bg-state-danger-50 p-4 text-state-danger-800 dark:border-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">
            <p class="font-medium">Failed to load task models</p>
            <p class="mt-1 text-sm/6">Please check your connection and try again.</p>
            <button
              type="button"
              (click)="slotsResource.reload()"
              class="mt-3 text-sm/6 font-medium underline hover:no-underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-500"
            >
              Retry
            </button>
          </div>
        } @else {
          <div class="flex flex-col gap-4">
            @for (task of slots(); track task.slot) {
              <app-task-model-card [task]="task" />
            }
          </div>
        }
      </div>
    </div>
  `,
})
export class TaskModelsPage {
  private readonly service = inject(TaskModelsService);

  protected readonly slotsResource = this.service.slotsResource;
  protected readonly slots = this.service.slots;
  protected readonly isInitialLoad = computed(
    () => this.slotsResource.isLoading() && this.slots().length === 0,
  );
}
