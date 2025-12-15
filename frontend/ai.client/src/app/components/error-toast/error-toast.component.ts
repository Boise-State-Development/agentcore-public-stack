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
    <div class="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-md">
      @for (error of visibleErrors(); track error.id) {
        <div
          class="bg-red-50 dark:bg-red-900/20 border-l-4 border-red-500 p-4 rounded-sm shadow-lg"
          role="alert"
          [attr.aria-live]="'assertive'"
        >
          <div class="flex items-start gap-3">
            <!-- Error Icon -->
            <div class="shrink-0">
              <svg class="size-5 text-red-500" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.28 7.22a.75.75 0 00-1.06 1.06L8.94 10l-1.72 1.72a.75.75 0 101.06 1.06L10 11.06l1.72 1.72a.75.75 0 101.06-1.06L11.06 10l1.72-1.72a.75.75 0 00-1.06-1.06L10 8.94 8.28 7.22z" clip-rule="evenodd" />
              </svg>
            </div>

            <!-- Error Content -->
            <div class="flex-1 min-w-0">
              <h3 class="text-sm/5 font-medium text-red-800 dark:text-red-200">
                {{ error.title }}
              </h3>
              <p class="mt-1 text-sm/5 text-red-700 dark:text-red-300">
                {{ error.message }}
              </p>

              @if (error.detail) {
                <details class="mt-2">
                  <summary class="text-xs text-red-600 dark:text-red-400 cursor-pointer hover:underline">
                    Show details
                  </summary>
                  <p class="mt-1 text-xs text-red-600 dark:text-red-400 font-mono whitespace-pre-wrap">
                    {{ error.detail }}
                  </p>
                </details>
              }

              @if (error.actionLabel && error.actionCallback) {
                <button
                  type="button"
                  (click)="error.actionCallback()"
                  class="mt-2 text-sm/5 font-medium text-red-600 dark:text-red-400 hover:text-red-500 dark:hover:text-red-300"
                >
                  {{ error.actionLabel }}
                </button>
              }
            </div>

            <!-- Dismiss Button -->
            @if (error.dismissible) {
              <button
                type="button"
                (click)="dismissError(error.id)"
                class="shrink-0 text-red-400 hover:text-red-500 dark:text-red-500 dark:hover:text-red-400"
                [attr.aria-label]="'Dismiss error'"
              >
                <svg class="size-5" fill="currentColor" viewBox="0 0 20 20">
                  <path d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z" />
                </svg>
              </button>
            }
          </div>
        </div>
      }
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
