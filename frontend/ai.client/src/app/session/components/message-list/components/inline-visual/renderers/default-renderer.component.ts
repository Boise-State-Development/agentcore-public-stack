import { Component, input, computed, ChangeDetectionStrategy } from '@angular/core';

/**
 * Fallback renderer for unknown visual types.
 * Displays a warning with the raw payload data.
 */
@Component({
  selector: 'app-default-renderer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="rounded-lg border border-state-warning-200 dark:border-state-warning-700
                bg-state-warning-50 dark:bg-state-warning-900/20 p-4">
      <div class="flex items-start gap-3">
        <svg class="size-5 text-state-warning-600 dark:text-state-warning-400 shrink-0 mt-0.5"
             fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        <div class="min-w-0 flex-1">
          <p class="text-sm font-medium text-state-warning-800 dark:text-state-warning-200">
            Unknown visual type: {{ uiType() }}
          </p>
          <pre class="mt-2 text-xs text-state-warning-700 dark:text-state-warning-300 overflow-x-auto whitespace-pre-wrap">{{ formattedPayload() }}</pre>
        </div>
      </div>
    </div>
  `
})
export class DefaultRendererComponent {
  /** The payload data to display */
  payload = input.required<unknown>();

  /** The unknown UI type */
  uiType = input.required<string>();

  /** Formatted payload for display */
  formattedPayload = computed(() => {
    try {
      return JSON.stringify(this.payload(), null, 2);
    } catch {
      return String(this.payload());
    }
  });
}
