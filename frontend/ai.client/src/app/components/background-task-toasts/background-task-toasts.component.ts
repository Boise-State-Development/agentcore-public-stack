import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { Router } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowRight,
  heroCheckCircle,
  heroExclamationTriangle,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import { BackgroundTask, BackgroundTaskService } from '../../services/background-tasks/background-task.service';

/**
 * Shell-level stack of persistent background-task toasts (top-right). Mounted
 * once in the app shell so it survives navigation between views — a headless
 * "Run now" kicked off on the schedules form keeps reporting here after the
 * user moves elsewhere.
 *
 * Unobtrusive but always visible: our signature pulsing dot while processing,
 * a check/warning when settled, and a "View" button that links to the result
 * (e.g. the session-detail conversation). Native `animate.enter` /
 * `animate.leave` (Angular 21) give it polished entry/exit animations.
 */
@Component({
  selector: 'app-background-task-toasts',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({ heroArrowRight, heroCheckCircle, heroExclamationTriangle, heroXMark }),
  ],
  host: { class: 'contents' },
  template: `
    <div
      aria-live="polite"
      aria-atomic="false"
      class="pointer-events-none fixed right-4 top-4 z-50 flex max-w-[calc(100vw-2rem)] flex-col items-end gap-3"
    >
      @for (task of taskService.tasks(); track task.id) {
        <div
          role="status"
          animate.enter="bg-toast-enter"
          animate.leave="bg-toast-leave"
          class="bg-toast pointer-events-auto w-72 max-w-full overflow-hidden rounded-xl bg-white/90 shadow-lg ring-1 ring-black/5 backdrop-blur-md dark:bg-gray-800/90 dark:ring-white/10"
        >
          <div class="flex items-center gap-2.5 px-3 py-2.5">
            <!-- Status glyph -->
            <div class="flex size-4 shrink-0 items-center justify-center">
              @switch (task.status) {
                @case ('processing') {
                  <span class="bg-toast-dot" aria-hidden="true"></span>
                }
                @case ('completed') {
                  <ng-icon name="heroCheckCircle" class="size-4 text-state-success-500 dark:text-state-success-400" aria-hidden="true" />
                }
                @case ('error') {
                  <ng-icon name="heroExclamationTriangle" class="size-4 text-state-warning-500 dark:text-state-warning-400" aria-hidden="true" />
                }
              }
            </div>

            <!-- Content -->
            <div class="min-w-0 flex-1">
              <p class="truncate text-sm font-medium text-gray-900 dark:text-white">{{ task.title }}</p>
              @if (task.detail) {
                <p class="truncate text-xs text-gray-500 dark:text-gray-400">{{ task.detail }}</p>
              }
              @if (task.route) {
                <button
                  type="button"
                  (click)="view(task)"
                  class="mt-1 inline-flex items-center gap-1 text-xs font-semibold text-primary-accessible hover:underline focus:outline-2 focus:outline-offset-2 focus:outline-primary-500 dark:text-primary-accessible-dark"
                >
                  {{ task.viewLabel ?? 'View' }}
                  <ng-icon name="heroArrowRight" class="size-4" aria-hidden="true" />
                </button>
              }
            </div>

            <!-- Dismiss -->
            <button
              type="button"
              (click)="dismiss(task.id)"
              class="-m-1 shrink-0 self-start rounded-md p-1 text-gray-400 hover:text-gray-600 focus:outline-2 focus:outline-offset-2 focus:outline-gray-400 dark:text-gray-500 dark:hover:text-gray-300"
              aria-label="Dismiss"
            >
              <ng-icon name="heroXMark" class="size-4" aria-hidden="true" />
            </button>
          </div>
        </div>
      }
    </div>
  `,
  styles: `
    @reference "../../../styles/theme.css";

    .bg-toast-enter {
      animation: bg-toast-in 260ms cubic-bezier(0.16, 1, 0.3, 1);
    }
    .bg-toast-leave {
      animation: bg-toast-out 200ms ease-in forwards;
    }

    @keyframes bg-toast-in {
      from { opacity: 0; transform: translateX(1rem) scale(0.98); }
      to { opacity: 1; transform: translateX(0) scale(1); }
    }
    @keyframes bg-toast-out {
      from { opacity: 1; transform: translateX(0) scale(1); }
      to { opacity: 0; transform: translateX(1rem) scale(0.98); }
    }

    /* Signature pulsing dot — ring + core, matching PulsatingLoaderComponent */
    .bg-toast-dot {
      position: relative;
      display: block;
      width: 7px;
      height: 7px;
      flex-shrink: 0;
    }
    .bg-toast-dot::before {
      content: '';
      position: absolute;
      left: 50%;
      top: 50%;
      width: 300%;
      height: 300%;
      transform: translate(-50%, -50%);
      border-radius: 50%;
      background-color: var(--color-state-info-500);
      animation: bg-toast-pulse-ring 1.25s cubic-bezier(0.215, 0.61, 0.355, 1) infinite;
    }
    .bg-toast-dot::after {
      content: '';
      position: absolute;
      left: 0;
      top: 0;
      width: 100%;
      height: 100%;
      border-radius: 50%;
      background-color: var(--color-state-info-500);
      animation: bg-toast-pulse-dot 1.25s cubic-bezier(0.455, 0.03, 0.515, 0.955) -0.4s infinite;
    }
    :host-context(.dark) .bg-toast-dot::before,
    :host-context(.dark) .bg-toast-dot::after {
      background-color: var(--color-state-info-400);
    }

    @keyframes bg-toast-pulse-ring {
      0% { transform: translate(-50%, -50%) scale(0.33); }
      80%, 100% { opacity: 0; }
    }
    @keyframes bg-toast-pulse-dot {
      0% { transform: scale(0.8); }
      50% { transform: scale(1); }
      100% { transform: scale(0.8); }
    }

    @media (prefers-reduced-motion: reduce) {
      .bg-toast-enter,
      .bg-toast-leave,
      .bg-toast-dot::before,
      .bg-toast-dot::after {
        animation: none;
      }
    }
  `,
})
export class BackgroundTaskToastsComponent {
  protected readonly taskService = inject(BackgroundTaskService);
  private readonly router = inject(Router);

  protected view(task: BackgroundTask): void {
    if (task.onView) {
      task.onView();
    } else if (task.route) {
      this.router.navigate(task.route);
    }
    this.taskService.dismiss(task.id);
  }

  protected dismiss(id: string): void {
    this.taskService.dismiss(id);
  }
}
