import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArchiveBox } from '@ng-icons/heroicons/outline';
import { Project } from '../models/project.model';

/**
 * The frame every project settings page shares: the breadcrumb back to the project
 * (`Projects / Name / Files`, as Memory has it), the page's title and description
 * with room for its actions beside them, and the loading and not-found states.
 *
 * Projected content is created by the page whether or not the shell shows it, so a
 * page wraps its content in its own `@if (state.project(); as p)`. Actions go in a
 * block of their own with a single root element: Angular only routes a control-flow
 * block to the `[shellActions]` slot when the block has exactly one root.
 *
 * ```html
 * <app-project-settings-shell [project]="state.project()" [loading]="state.loading()" [error]="state.error()" heading="Files">
 *   @if (state.canEdit()) {
 *     <div shellActions class="flex items-center gap-3"><button …>Save</button></div>
 *   }
 *   @if (state.project(); as p) {
 *     <app-project-files [project]="p" />
 *   }
 * </app-project-settings-shell>
 * ```
 */
@Component({
  selector: 'app-project-settings-shell',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink],
  providers: [provideIcons({ heroArchiveBox })],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-7xl px-4 py-5 sm:px-6 lg:px-8">
        <nav aria-label="Breadcrumb" class="flex min-w-0 items-center gap-1 text-sm/6">
          <a
            routerLink="/projects"
            class="shrink-0 rounded-lg px-1.5 py-0.5 text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-white/5 dark:hover:text-white"
          >
            Projects
          </a>
          @if (project(); as p) {
            <span class="shrink-0 text-gray-400 dark:text-gray-600" aria-hidden="true">/</span>
            <a
              [routerLink]="['/projects', p.projectId, 'overview']"
              class="min-w-0 truncate rounded-lg px-1.5 py-0.5 text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-white/5 dark:hover:text-white"
            >
              {{ p.name }}
            </a>
            <span class="shrink-0 text-gray-400 dark:text-gray-600" aria-hidden="true">/</span>
            <span class="shrink-0 px-1.5 py-0.5 font-medium text-gray-900 dark:text-white" aria-current="page">{{ heading() }}</span>
          }
        </nav>

        @if (loading()) {
          <div class="mx-auto mt-8 max-w-3xl animate-pulse" aria-busy="true">
            <div class="h-8 w-40 rounded bg-gray-200 dark:bg-gray-700"></div>
            <div class="mt-3 h-4 w-2/3 rounded bg-gray-100 dark:bg-gray-800"></div>
            <div class="mt-8 h-48 rounded-2xl bg-gray-100 dark:bg-gray-800"></div>
          </div>
        } @else if (error()) {
          <div class="mt-10 rounded-2xl border border-dashed border-gray-300 bg-white p-12 text-center dark:border-gray-700 dark:bg-gray-800">
            <p role="alert" class="text-sm/6 text-gray-600 dark:text-gray-400">{{ error() }}</p>
          </div>
        } @else if (project(); as p) {
          <div class="mx-auto max-w-3xl">
            <div class="mt-6 flex flex-wrap items-start justify-between gap-4 sm:flex-nowrap">
              <div class="min-w-0 flex-1 basis-full sm:basis-0">
                <h1 class="text-2xl font-semibold tracking-tight text-gray-900 sm:text-3xl dark:text-white">{{ heading() }}</h1>
                @if (description()) {
                  <p class="mt-1 max-w-2xl text-sm/6 text-gray-600 dark:text-gray-400">{{ description() }}</p>
                }
              </div>
              <div data-shell-actions class="flex shrink-0 flex-wrap items-center gap-3">
                <ng-content select="[shellActions]" />
              </div>
            </div>

            @if (p.status === 'archived') {
              <div role="status" class="mt-5 flex items-start gap-3 rounded-2xl border border-state-warning-200 bg-state-warning-50 px-4 py-3 text-sm/6 text-state-warning-700 dark:border-state-warning-800 dark:bg-state-warning-900/20 dark:text-state-warning-400">
                <ng-icon name="heroArchiveBox" class="mt-0.5 size-5 shrink-0" aria-hidden="true" />
                <p>This project is archived, so nothing here can be changed.</p>
              </div>
            }

            <div class="mt-8">
              <ng-content />
            </div>
          </div>
        }
      </div>
    </div>
  `,
})
export class ProjectSettingsShellComponent {
  readonly project = input.required<Project | null>();
  readonly loading = input.required<boolean>();
  readonly error = input<string | null>(null);
  /** The page's name, in the breadcrumb and as its title. */
  readonly heading = input.required<string>();
  readonly description = input<string | null>(null);
}
