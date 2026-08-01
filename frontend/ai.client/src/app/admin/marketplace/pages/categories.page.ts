import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  OnInit,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroPlus, heroTrash, heroTag } from '@ng-icons/heroicons/outline';
import { TooltipDirective } from '../../../components/tooltip/tooltip.directive';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import { AgentCategory } from '../models/marketplace.model';

/**
 * Categories — the browse order of the store (D10).
 *
 * Admin-managed records rather than a build-time constant: a category set that needs a
 * deploy to change will not be maintained.
 *
 * Two rules the UI has to make legible, because both are surprising otherwise:
 *
 * - **A category cannot be renamed into a different id.** The id is half of the directory
 *   partition key, so it is fixed at creation; editing changes the label only. The row
 *   shows the id when it differs from the label so the difference is visible rather than
 *   discovered.
 * - **Disable, don't delete.** Deleting is refused (409) while listings reference the
 *   category. Disabling drops it from the pickers and the browse header while the agents
 *   already in it keep working — nearly always what was actually meant.
 */
@Component({
  selector: 'app-marketplace-categories',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, TooltipDirective],
  providers: [provideIcons({ heroPlus, heroTrash, heroTag })],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-4xl px-4 py-8 sm:px-6 lg:px-8">
        <div class="mb-6">
          <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">Categories</h1>
          <p class="mt-1 max-w-2xl text-sm/6 text-gray-600 dark:text-gray-400">
            How the store is grouped, in browse order. Empty categories hide themselves on
            Discover, so it is safe to add one before anything is published into it.
          </p>
        </div>

        @if (error()) {
          <div
            role="alert"
            class="mb-4 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm/6 text-rose-800 dark:border-rose-900 dark:bg-rose-900/20 dark:text-rose-300"
          >
            {{ error() }}
          </div>
        }

        <!-- Add -->
        <div
          class="mb-6 flex flex-col gap-2 rounded-2xl border border-gray-200 bg-white p-4 sm:flex-row sm:items-end dark:border-gray-700 dark:bg-gray-800"
        >
          <div class="flex-1">
            <label for="new-category" class="block text-sm/6 font-medium text-gray-900 dark:text-white">
              New category
            </label>
            <input
              type="text"
              id="new-category"
              [value]="newLabel()"
              (input)="onNewLabelInput($event)"
              placeholder="e.g. Student Support"
              class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-500"
            />
          </div>
          <button
            type="button"
            [disabled]="!newLabel().trim() || busy()"
            (click)="addCategory()"
            class="inline-flex shrink-0 items-center gap-2 rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
          >
            <ng-icon name="heroPlus" class="size-5" aria-hidden="true" />
            Add
          </button>
        </div>

        @if (loading()) {
          <div class="flex items-center justify-center py-16">
            <div
              class="size-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-gray-600 dark:border-t-blue-400"
            ></div>
            <span class="sr-only">Loading categories</span>
          </div>
        } @else if (categories().length === 0) {
          <div
            class="rounded-2xl border border-dashed border-gray-300 px-6 py-16 text-center dark:border-gray-600"
          >
            <ng-icon
              name="heroTag"
              class="mx-auto size-8 text-gray-400 dark:text-gray-500"
              aria-hidden="true"
            />
            <h2 class="mt-3 text-sm/6 font-semibold text-gray-900 dark:text-white">
              No categories yet
            </h2>
          </div>
        } @else {
          <ul class="flex flex-col gap-2">
            @for (category of categories(); track category.id) {
              <li
                class="flex flex-col gap-3 rounded-2xl border border-gray-200 bg-white p-4 sm:flex-row sm:items-center dark:border-gray-700 dark:bg-gray-800"
              >
                <div class="min-w-0 flex-1">
                  <label [for]="'label-' + category.id" class="sr-only">
                    Label for {{ category.label }}
                  </label>
                  <input
                    type="text"
                    [id]="'label-' + category.id"
                    [value]="category.label"
                    (change)="renameCategory(category, $event)"
                    class="block w-full rounded-2xl border border-transparent bg-transparent px-2 py-1 text-sm/6 font-medium text-gray-900 hover:border-gray-300 focus:border-blue-500 focus:bg-white focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white dark:hover:border-gray-600 dark:focus:bg-gray-900"
                  />
                  @if (category.id !== category.label) {
                    <p class="px-2 text-xs text-gray-400 dark:text-gray-500">
                      id: {{ category.id }} — fixed at creation
                    </p>
                  }
                </div>

                <div class="flex shrink-0 items-center gap-2">
                  <button
                    type="button"
                    [disabled]="busy()"
                    (click)="toggleEnabled(category)"
                    class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                    [appTooltip]="
                      category.enabled
                        ? 'Hide from the pickers and the browse header. Agents already in it keep working.'
                        : 'Offer this category again'
                    "
                    appTooltipPosition="top"
                  >
                    {{ category.enabled ? 'Enabled' : 'Disabled' }}
                  </button>
                  <button
                    type="button"
                    [disabled]="busy()"
                    (click)="removeCategory(category)"
                    class="rounded-2xl p-2 text-gray-500 hover:bg-rose-50 hover:text-rose-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-500 disabled:cursor-not-allowed disabled:opacity-50 dark:text-gray-400 dark:hover:bg-rose-900/20 dark:hover:text-rose-400"
                    [appTooltip]="'Delete category'"
                    appTooltipPosition="top"
                  >
                    <ng-icon name="heroTrash" class="size-5" aria-hidden="true" />
                    <span class="sr-only">Delete {{ category.label }}</span>
                  </button>
                </div>
              </li>
            }
          </ul>
        }
      </div>
    </div>
  `,
})
export class MarketplaceCategoriesPage implements OnInit {
  private service = inject(AdminMarketplaceService);

  readonly categories = signal<AgentCategory[]>([]);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly busy = signal(false);
  readonly newLabel = signal('');

  ngOnInit(): void {
    void this.reload();
  }

  onNewLabelInput(event: Event): void {
    this.newLabel.set((event.target as HTMLInputElement).value);
  }

  async reload(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      this.categories.set(await this.service.loadCategories());
    } catch (err) {
      this.error.set(this.messageFor(err, 'Failed to load categories.'));
    } finally {
      this.loading.set(false);
    }
  }

  async addCategory(): Promise<void> {
    const label = this.newLabel().trim();
    if (!label) return;
    await this.mutate(async () => {
      await this.service.createCategory({ label, order: this.categories().length * 10 });
      this.newLabel.set('');
    });
  }

  async renameCategory(category: AgentCategory, event: Event): Promise<void> {
    const label = (event.target as HTMLInputElement).value.trim();
    if (!label || label === category.label) return;
    await this.mutate(() => this.service.updateCategory(category.id, { label }));
  }

  async toggleEnabled(category: AgentCategory): Promise<void> {
    await this.mutate(() =>
      this.service.updateCategory(category.id, { enabled: !category.enabled }),
    );
  }

  async removeCategory(category: AgentCategory): Promise<void> {
    await this.mutate(() => this.service.deleteCategory(category.id));
  }

  /** Run a mutation, surface its message, and reload so the list matches the server. */
  private async mutate(action: () => Promise<unknown>): Promise<void> {
    this.busy.set(true);
    this.error.set(null);
    try {
      await action();
      await this.reload();
    } catch (err) {
      // The in-use delete refusal (409) explains itself and suggests disabling instead,
      // so surface the server's message rather than a generic one.
      //
      // ⚠️ Set *after* the reload, not before: ``reload`` clears the error banner on entry,
      // so the obvious order silently swallows every message this branch exists to show.
      const message = this.messageFor(err, 'That change could not be saved.');
      await this.reload();
      this.error.set(message);
    } finally {
      this.busy.set(false);
    }
  }

  private messageFor(err: unknown, fallback: string): string {
    const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
    return typeof detail === 'string' ? detail : fallback;
  }
}
