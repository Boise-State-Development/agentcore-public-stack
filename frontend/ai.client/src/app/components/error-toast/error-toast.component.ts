import { Component, ChangeDetectionStrategy, inject, computed } from '@angular/core';
import { ErrorService, ErrorMessage } from '../../services/error/error.service';

/**
 * Error toast component that displays error messages from ErrorService
 *
 * Shows errors as dismissible toast notifications in the bottom-right corner
 * Automatically stacks multiple errors
 */
@Component({
  selector: 'app-error-toast',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div aria-live="assertive" class="pointer-events-none fixed inset-0 z-50 flex items-end px-4 py-6 sm:items-start sm:p-6">
      <div class="flex w-full flex-col items-center space-y-4 sm:items-end">
        @for (error of visibleErrors(); track error.id) {
          <div
            class="pointer-events-auto w-full max-w-sm translate-y-0 transform rounded-lg bg-white opacity-100 shadow-lg outline-1 outline-black/5 transition duration-300 ease-out sm:translate-x-0 dark:bg-gray-800 dark:-outline-offset-1 dark:outline-white/10 starting:translate-y-2 starting:opacity-0 starting:sm:translate-x-2 starting:sm:translate-y-0"
            role="alert"
          >
            <div class="p-4">
              <div class="flex items-start">
                <!-- Error Icon -->
                <div class="shrink-0">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" data-slot="icon" aria-hidden="true" class="size-6 text-red-400">
                    <path d="m9.75 9.75 4.5 4.5m0-4.5-4.5 4.5M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" stroke-linecap="round" stroke-linejoin="round" />
                  </svg>
                </div>

                <!-- Error Content -->
                <div class="ml-3 w-0 flex-1 pt-0.5">
                  <p class="text-sm font-medium text-gray-900 dark:text-white">
                    {{ error.title }}
                  </p>
                  <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                    {{ error.message }}
                  </p>

                  @if (error.detail) {
                    <details class="mt-2">
                      <summary class="text-xs text-gray-600 dark:text-gray-400 cursor-pointer hover:underline">
                        Show details
                      </summary>
                      <p class="mt-1 text-xs text-gray-600 dark:text-gray-400 font-mono whitespace-pre-wrap">
                        {{ error.detail }}
                      </p>
                    </details>
                  }

                  @if (error.actionLabel && error.actionCallback) {
                    <button
                      type="button"
                      (click)="error.actionCallback()"
                      class="mt-2 text-sm font-medium text-gray-900 dark:text-white hover:text-gray-700 dark:hover:text-gray-300 focus:outline-2 focus:outline-offset-2 focus:outline-red-600 dark:focus:outline-red-500"
                    >
                      {{ error.actionLabel }}
                    </button>
                  }
                </div>

                <!-- Dismiss Button -->
                @if (error.dismissible) {
                  <div class="ml-4 flex shrink-0">
                    <button
                      type="button"
                      (click)="dismissError(error.id)"
                      class="inline-flex rounded-md text-gray-400 hover:text-gray-500 focus:outline-2 focus:outline-offset-2 focus:outline-red-600 dark:hover:text-white dark:focus:outline-red-500"
                      [attr.aria-label]="'Dismiss error'"
                    >
                      <span class="sr-only">Close</span>
                      <svg viewBox="0 0 20 20" fill="currentColor" data-slot="icon" aria-hidden="true" class="size-5">
                        <path d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z" />
                      </svg>
                    </button>
                  </div>
                }
              </div>
            </div>
          </div>
        }
      </div>
    </div>
  `,
  styles: [`
    :host {
      display: contents;
    }
  `]
})
export class ErrorToastComponent {
  private errorService = inject(ErrorService);

  // Only show errors from the last 10 seconds
  visibleErrors = computed(() => {
    const now = new Date();
    const tenSecondsAgo = new Date(now.getTime() - 10000);

    return this.errorService.errorMessages()
      .filter(error => error.timestamp > tenSecondsAgo);
  });

  dismissError(id: string): void {
    this.errorService.dismissError(id);
  }
}
