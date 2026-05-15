import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroPencil,
  heroTrash,
  heroPlus,
  heroArrowTopRightOnSquare,
  heroDocumentText,
} from '@ng-icons/heroicons/outline';
import { UserMenuLinksService } from './services/user-menu-links.service';
import { UserMenuLink } from './models/user-menu-link.model';

@Component({
  selector: 'app-manage-user-menu-links-page',
  imports: [RouterLink, NgIcon],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroPencil,
      heroTrash,
      heroPlus,
      heroArrowTopRightOnSquare,
      heroDocumentText,
    }),
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div>
      <div class="mb-8 flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h1 class="text-3xl/9 font-bold text-gray-900 dark:text-white">User Menu Links</h1>
            <p class="mt-1 text-gray-600 dark:text-gray-400">
              Manage links rendered in the user menu. Each link opens an external URL or an in-app modal with rich text.
            </p>
          </div>
          <a
            routerLink="/admin/manage-user-menu-links/new"
            class="inline-flex items-center gap-2 rounded-sm bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:bg-blue-500 dark:hover:bg-blue-600"
          >
            <ng-icon name="heroPlus" class="size-5" />
            New link
          </a>
        </div>

        @if (loadError()) {
          <div class="mb-4 rounded-sm border border-red-300 bg-red-50 p-4 text-sm/6 text-red-700 dark:border-red-700 dark:bg-red-900/20 dark:text-red-300">
            Failed to load links. {{ loadError() }}
          </div>
        }

        @if (links().length === 0 && !isLoading()) {
          <div class="rounded-sm border border-gray-300 bg-white p-12 text-center dark:border-gray-600 dark:bg-gray-800">
            <p class="text-base/7 text-gray-500 dark:text-gray-400">No user-menu links yet.</p>
            <a
              routerLink="/admin/manage-user-menu-links/new"
              class="mt-4 inline-flex items-center gap-2 text-sm/6 font-medium text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
            >
              Add the first one →
            </a>
          </div>
        } @else {
          <div class="space-y-3">
            @for (link of links(); track link.link_id) {
              <div class="flex items-center justify-between gap-4 rounded-sm border border-gray-300 bg-white p-4 dark:border-gray-600 dark:bg-gray-800">
                <div class="flex flex-1 items-center gap-3 min-w-0">
                  <ng-icon
                    [name]="link.kind === 'external' ? 'heroArrowTopRightOnSquare' : 'heroDocumentText'"
                    class="size-5 shrink-0 text-gray-400 dark:text-gray-500"
                  />
                  <div class="min-w-0 flex-1">
                    <div class="flex items-center gap-2">
                      <span class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">{{ link.label }}</span>
                      @if (!link.enabled) {
                        <span class="shrink-0 rounded-sm bg-gray-100 px-2 py-0.5 text-xs/5 font-medium text-gray-600 dark:bg-gray-700 dark:text-gray-300">Disabled</span>
                      }
                      <span class="shrink-0 rounded-sm bg-blue-100 px-2 py-0.5 text-xs/5 font-medium text-blue-700 dark:bg-blue-900/40 dark:text-blue-300">
                        {{ link.kind === 'external' ? 'External' : 'Modal' }}
                      </span>
                    </div>
                    @if (link.kind === 'external') {
                      <p class="mt-0.5 truncate text-xs/5 text-gray-500 dark:text-gray-400">{{ link.url }}</p>
                    } @else {
                      <p class="mt-0.5 truncate text-xs/5 text-gray-500 dark:text-gray-400">{{ summarize(link.body_markdown) }}</p>
                    }
                  </div>
                </div>
                <div class="flex shrink-0 items-center gap-2">
                  <span class="hidden text-xs text-gray-400 sm:inline dark:text-gray-500" [title]="'Order: ' + link.order">#{{ link.order }}</span>
                  <a
                    [routerLink]="['/admin/manage-user-menu-links/edit', link.link_id]"
                    class="inline-flex items-center gap-1 rounded-sm border border-gray-300 bg-white px-2.5 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus:outline-none focus:ring-2 focus:ring-gray-500 dark:border-gray-500 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
                    [attr.aria-label]="'Edit ' + link.label"
                  >
                    <ng-icon name="heroPencil" class="size-4" />
                    <span class="sr-only sm:not-sr-only">Edit</span>
                  </a>
                  <button
                    type="button"
                    (click)="onDelete(link)"
                    class="inline-flex items-center gap-1 rounded-sm border border-red-300 bg-white px-2.5 py-1.5 text-sm/6 font-medium text-red-700 hover:bg-red-50 focus:outline-none focus:ring-2 focus:ring-red-500 dark:border-red-500 dark:bg-gray-700 dark:text-red-400 dark:hover:bg-red-900/20"
                    [attr.aria-label]="'Delete ' + link.label"
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
export class ManageUserMenuLinksPage {
  private readonly service = inject(UserMenuLinksService);

  protected readonly links = computed<UserMenuLink[]>(
    () => this.service.adminLinksResource.value()?.links ?? [],
  );
  protected readonly isLoading = computed(() => this.service.adminLinksResource.isLoading());
  protected readonly loadError = computed(() => {
    const err = this.service.adminLinksResource.error();
    if (!err) return null;
    return err instanceof Error ? err.message : String(err);
  });

  protected summarize(markdown: string | null | undefined): string {
    if (!markdown) return '(empty)';
    const stripped = markdown.replace(/[#*_`>\-]/g, '').replace(/\s+/g, ' ').trim();
    return stripped.length > 120 ? stripped.slice(0, 120) + '…' : stripped;
  }

  protected async onDelete(link: UserMenuLink): Promise<void> {
    if (!confirm(`Delete "${link.label}"?`)) return;
    try {
      await this.service.deleteLink(link.link_id);
    } catch (err) {
      console.error('Failed to delete user-menu link', err);
      alert('Failed to delete the link. Please try again.');
    }
  }
}
