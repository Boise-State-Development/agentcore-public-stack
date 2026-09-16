import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroPencil,
  heroTrash,
  heroPlus,
  heroCheckCircle,
  heroXCircle,
  heroArrowUp,
  heroArrowDown,
} from '@ng-icons/heroicons/outline';
import { AdminAgentTemplatesService } from './services/admin-agent-templates.service';
import { AgentTemplateAdmin } from './models/agent-template-admin.model';
import { ToastService } from '../../services/toast/toast.service';

/**
 * Admin list of agent templates: name/emoji/enabled/order with enable-disable,
 * delete, reorder (move up/down) and edit. Mirrors the `manage-system-prompts`
 * list and the `manage-models` in-place toggle pattern.
 *
 * Reorder is expressed as `sort_order` PATCHes on the two swapped rows — the
 * backend on this branch exposes no dedicated reorder endpoint, so adjacency
 * swaps keep the persisted order consistent with what the picker reads.
 */
@Component({
  selector: 'app-manage-agent-templates-page',
  imports: [RouterLink, NgIcon],
  providers: [
    provideIcons({
      heroPencil,
      heroTrash,
      heroPlus,
      heroCheckCircle,
      heroXCircle,
      heroArrowUp,
      heroArrowDown,
    }),
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div>
      <div class="mb-8 flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 class="text-3xl/9 font-bold text-gray-900 dark:text-white">Agent Templates</h1>
          <p class="mt-1 text-gray-600 dark:text-gray-400">
            Curated starting points users pick from when creating a new agent. Instructions,
            guardrails and tool bindings here are <em>examples to adapt</em> — edit them freely
            for your organization. Disabled templates are hidden from the picker.
          </p>
        </div>
        <a
          routerLink="/admin/agent-templates/new"
          class="inline-flex items-center gap-2 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus:outline-none focus:ring-2 focus:ring-primary-500"
        >
          <ng-icon name="heroPlus" class="size-5" />
          New template
        </a>
      </div>

      @if (loadError()) {
        <div class="mb-4 rounded-sm border border-state-danger-300 bg-state-danger-50 p-4 text-sm/6 text-state-danger-700 dark:border-state-danger-700 dark:bg-state-danger-900/20 dark:text-state-danger-300">
          Failed to load templates. {{ loadError() }}
        </div>
      }

      @if (templates().length === 0 && !isLoading()) {
        <div class="rounded-sm border border-gray-300 bg-white p-12 text-center dark:border-gray-600 dark:bg-gray-800">
          <p class="text-base/7 text-gray-500 dark:text-gray-400">No agent templates yet.</p>
          <a
            routerLink="/admin/agent-templates/new"
            class="mt-4 inline-flex items-center gap-2 text-sm/6 font-medium text-primary-accessible hover:underline dark:text-primary-accessible-dark"
          >
            Add the first one →
          </a>
        </div>
      } @else {
        <div class="space-y-3">
          @for (tpl of templates(); track tpl.template_id; let i = $index) {
            <div class="flex items-start justify-between gap-4 rounded-sm border border-gray-300 bg-white p-4 dark:border-gray-600 dark:bg-gray-800">
              <div class="flex min-w-0 flex-1 items-start gap-3">
                <span class="shrink-0 text-2xl" aria-hidden="true">{{ tpl.emoji || '🧩' }}</span>
                <div class="min-w-0 flex-1">
                  <div class="flex items-center gap-2">
                    <span class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">{{ tpl.name }}</span>
                    @if (tpl.status === 'enabled') {
                      <span class="shrink-0 inline-flex items-center gap-1 rounded-sm bg-state-success-100 px-2 py-0.5 text-xs/5 font-medium text-state-success-700 dark:bg-state-success-900/40 dark:text-state-success-300">
                        <ng-icon name="heroCheckCircle" class="size-3.5" />
                        Enabled
                      </span>
                    } @else {
                      <span class="shrink-0 inline-flex items-center gap-1 rounded-sm bg-gray-100 px-2 py-0.5 text-xs/5 font-medium text-gray-600 dark:bg-gray-700 dark:text-gray-300">
                        <ng-icon name="heroXCircle" class="size-3.5" />
                        Disabled
                      </span>
                    }
                    <span class="shrink-0 text-xs/5 text-gray-400 dark:text-gray-500">#{{ tpl.sort_order }}</span>
                  </div>
                  <p class="mt-0.5 text-xs/5 text-gray-500 dark:text-gray-400">{{ tpl.pitch || tpl.description }}</p>
                </div>
              </div>
              <div class="flex shrink-0 items-center gap-2">
                <button
                  type="button"
                  (click)="moveUp(i)"
                  [disabled]="i === 0 || busy()"
                  class="inline-flex items-center rounded-2xl border border-gray-300 bg-white p-1.5 text-gray-700 hover:bg-gray-100 focus:outline-none focus:ring-2 focus:ring-gray-500 disabled:cursor-not-allowed disabled:opacity-40 dark:border-gray-500 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
                  [attr.aria-label]="'Move ' + tpl.name + ' up'"
                >
                  <ng-icon name="heroArrowUp" class="size-4" />
                </button>
                <button
                  type="button"
                  (click)="moveDown(i)"
                  [disabled]="i === templates().length - 1 || busy()"
                  class="inline-flex items-center rounded-2xl border border-gray-300 bg-white p-1.5 text-gray-700 hover:bg-gray-100 focus:outline-none focus:ring-2 focus:ring-gray-500 disabled:cursor-not-allowed disabled:opacity-40 dark:border-gray-500 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
                  [attr.aria-label]="'Move ' + tpl.name + ' down'"
                >
                  <ng-icon name="heroArrowDown" class="size-4" />
                </button>
                <button
                  type="button"
                  (click)="toggleEnabled(tpl)"
                  [disabled]="busy()"
                  class="inline-flex items-center gap-1 rounded-2xl border border-gray-300 bg-white px-2.5 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus:outline-none focus:ring-2 focus:ring-gray-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-500 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
                  [attr.aria-label]="(tpl.status === 'enabled' ? 'Disable ' : 'Enable ') + tpl.name"
                >
                  {{ tpl.status === 'enabled' ? 'Disable' : 'Enable' }}
                </button>
                <a
                  [routerLink]="['/admin/agent-templates/edit', tpl.template_id]"
                  class="inline-flex items-center gap-1 rounded-2xl border border-gray-300 bg-white px-2.5 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus:outline-none focus:ring-2 focus:ring-gray-500 dark:border-gray-500 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
                  [attr.aria-label]="'Edit ' + tpl.name"
                >
                  <ng-icon name="heroPencil" class="size-4" />
                  <span class="sr-only sm:not-sr-only">Edit</span>
                </a>
                <button
                  type="button"
                  (click)="onDelete(tpl)"
                  [disabled]="busy()"
                  class="inline-flex items-center gap-1 rounded-2xl border border-state-danger-300 bg-white px-2.5 py-1.5 text-sm/6 font-medium text-state-danger-700 hover:bg-state-danger-50 focus:outline-none focus:ring-2 focus:ring-state-danger-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-state-danger-500 dark:bg-gray-700 dark:text-state-danger-400 dark:hover:bg-state-danger-900/20"
                  [attr.aria-label]="'Delete ' + tpl.name"
                >
                  <ng-icon name="heroTrash" class="size-4" />
                  <span class="sr-only sm:not-sr-only">Delete</span>
                </button>
              </div>
            </div>
          }
        </div>
      }
    </div>
  `,
})
export class ManageAgentTemplatesPage {
  private readonly service = inject(AdminAgentTemplatesService);
  private readonly toast = inject(ToastService);

  /** True while an enable/disable, reorder or delete request is in flight. */
  protected readonly busy = signal(false);

  constructor() {
    this.service.ensureLoaded();
  }

  /** Templates in display order: `sort_order` ascending, then name. */
  protected readonly templates = computed<AgentTemplateAdmin[]>(() => {
    const list = this.service.templatesResource.value()?.templates ?? [];
    return [...list].sort(
      (a, b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name),
    );
  });

  protected readonly isLoading = computed(() =>
    this.service.templatesResource.isLoading(),
  );

  protected readonly loadError = computed(() => {
    const err = this.service.templatesResource.error();
    if (!err) return null;
    return err instanceof Error ? err.message : String(err);
  });

  protected async toggleEnabled(tpl: AgentTemplateAdmin): Promise<void> {
    if (this.busy()) return;
    const next = tpl.status === 'enabled' ? 'disabled' : 'enabled';
    this.busy.set(true);
    try {
      await this.service.updateTemplate(tpl.template_id, { status: next });
    } catch (err) {
      console.error('Failed to toggle template', err);
      this.toast.error('Could not update template', 'Please try again.');
    } finally {
      this.busy.set(false);
    }
  }

  protected async moveUp(index: number): Promise<void> {
    await this.swapOrder(index, index - 1);
  }

  protected async moveDown(index: number): Promise<void> {
    await this.swapOrder(index, index + 1);
  }

  /** Swap the `sort_order` of two adjacent rows and persist both. */
  private async swapOrder(a: number, b: number): Promise<void> {
    if (this.busy()) return;
    const list = this.templates();
    if (a < 0 || b < 0 || a >= list.length || b >= list.length) return;
    const first = list[a];
    const second = list[b];
    this.busy.set(true);
    try {
      await this.service.updateTemplate(first.template_id, { sort_order: second.sort_order });
      await this.service.updateTemplate(second.template_id, { sort_order: first.sort_order });
    } catch (err) {
      console.error('Failed to reorder templates', err);
      this.toast.error('Could not reorder templates', 'Please try again.');
    } finally {
      this.busy.set(false);
    }
  }

  protected async onDelete(tpl: AgentTemplateAdmin): Promise<void> {
    if (this.busy()) return;
    if (!confirm(`Delete "${tpl.name}"? Users will no longer see it in the template picker.`)) return;
    this.busy.set(true);
    try {
      await this.service.deleteTemplate(tpl.template_id);
      this.toast.success('Deleted', `"${tpl.name}" was removed.`);
    } catch (err) {
      console.error('Failed to delete template', err);
      this.toast.error('Could not delete template', 'Please try again.');
    } finally {
      this.busy.set(false);
    }
  }
}
