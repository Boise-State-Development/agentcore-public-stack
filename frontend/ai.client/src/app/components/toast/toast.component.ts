import { Component, ChangeDetectionStrategy, inject } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroCheckCircle,
  heroExclamationCircle,
  heroExclamationTriangle,
  heroInformationCircle,
  heroXMark
} from '@ng-icons/heroicons/outline';
import { ToastService, ToastType } from '../../services/toast/toast.service';

/**
 * Toast notification component that displays messages from ToastService.
 *
 * Features:
 * - Four toast types: success, error, warning, info
 * - Accessible: ARIA live region, dismissible via keyboard
 * - Auto-dismiss with configurable duration
 * - Stacks multiple toasts
 * - Dark mode support
 * - Smooth enter/exit animations
 *
 * @example
 * ```html
 * <!-- Add to app.component.html -->
 * <app-toast />
 * ```
 */
@Component({
  selector: 'app-toast',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroCheckCircle,
      heroExclamationCircle,
      heroExclamationTriangle,
      heroInformationCircle,
      heroXMark
    })
  ],
  host: {
    'class': 'contents'
  },
  template: `
    <!-- Toast container - positioned at bottom-right -->
    <div
      aria-live="polite"
      aria-atomic="true"
      class="pointer-events-none fixed inset-0 z-50 flex flex-col items-end justify-end gap-3 p-4 sm:p-6"
    >
      @for (toast of toastService.toasts(); track toast.id) {
        <div
          role="alert"
          class="pointer-events-auto w-full max-w-sm overflow-hidden rounded-lg shadow-lg ring-1 ring-black/5 dark:ring-white/10"
          [class]="getToastContainerClass(toast.type)"
        >
          <div class="p-4">
            <div class="flex items-start">
              <!-- Icon -->
              <div class="shrink-0">
                <ng-icon
                  [name]="getIconName(toast.type)"
                  [class]="'size-5 ' + getIconClass(toast.type)"
                  aria-hidden="true"
                />
              </div>

              <!-- Content -->
              <div class="ml-3 w-0 flex-1">
                <p [class]="'text-sm font-medium ' + getTitleClass(toast.type)">
                  {{ toast.title }}
                </p>
                @if (toast.message) {
                  <p [class]="'mt-1 text-sm ' + getMessageClass(toast.type)">
                    {{ toast.message }}
                  </p>
                }
              </div>

              <!-- Dismiss button -->
              @if (toast.dismissible) {
                <div class="ml-4 flex shrink-0">
                  <button
                    type="button"
                    (click)="dismiss(toast.id)"
                    [class]="'inline-flex rounded-md focus:outline-2 focus:outline-offset-2 ' + getDismissButtonClass(toast.type)"
                    aria-label="Dismiss notification"
                  >
                    <span class="sr-only">Dismiss</span>
                    <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
                  </button>
                </div>
              }
            </div>
          </div>
        </div>
      }
    </div>
  `,
  styles: `
    @import "tailwindcss";

    @custom-variant dark (&:where(.dark, .dark *));

    /* Toast enter animation */
    [role="alert"] {
      animation: toast-enter 200ms ease-out;
    }

    @keyframes toast-enter {
      from {
        opacity: 0;
        transform: translateX(1rem);
      }
      to {
        opacity: 1;
        transform: translateX(0);
      }
    }
  `
})
export class ToastComponent {
  protected readonly toastService = inject(ToastService);

  /**
   * Dismiss a toast
   */
  protected dismiss(id: string): void {
    this.toastService.dismiss(id);
  }

  /**
   * Get container class based on toast type
   */
  protected getToastContainerClass(type: ToastType): string {
    const classes: Record<ToastType, string> = {
      success: 'bg-white dark:bg-gray-800',
      error: 'bg-white dark:bg-gray-800',
      warning: 'bg-white dark:bg-gray-800',
      info: 'bg-white dark:bg-gray-800'
    };
    return classes[type];
  }

  /**
   * Get icon name based on toast type
   */
  protected getIconName(type: ToastType): string {
    const icons: Record<ToastType, string> = {
      success: 'heroCheckCircle',
      error: 'heroExclamationCircle',
      warning: 'heroExclamationTriangle',
      info: 'heroInformationCircle'
    };
    return icons[type];
  }

  /**
   * Get icon class based on toast type
   */
  protected getIconClass(type: ToastType): string {
    const classes: Record<ToastType, string> = {
      success: 'text-green-500 dark:text-green-400',
      error: 'text-red-500 dark:text-red-400',
      warning: 'text-yellow-500 dark:text-yellow-400',
      info: 'text-blue-500 dark:text-blue-400'
    };
    return classes[type];
  }

  /**
   * Get title class based on toast type
   */
  protected getTitleClass(type: ToastType): string {
    return 'text-gray-900 dark:text-white';
  }

  /**
   * Get message class based on toast type
   */
  protected getMessageClass(type: ToastType): string {
    return 'text-gray-500 dark:text-gray-400';
  }

  /**
   * Get dismiss button class based on toast type
   */
  protected getDismissButtonClass(type: ToastType): string {
    const baseClass = 'text-gray-400 hover:text-gray-500 dark:text-gray-500 dark:hover:text-gray-400';
    const focusClasses: Record<ToastType, string> = {
      success: 'focus:outline-green-500',
      error: 'focus:outline-red-500',
      warning: 'focus:outline-yellow-500',
      info: 'focus:outline-blue-500'
    };
    return `${baseClass} ${focusClasses[type]}`;
  }
}
