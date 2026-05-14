import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { MarkdownComponent } from 'ngx-markdown';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark } from '@ng-icons/heroicons/outline';

export interface UserMenuLinkModalData {
  label: string;
  bodyMarkdown: string;
}

/**
 * Generic rich-text modal opened from admin-managed user-menu links.
 * Renders markdown via ngx-markdown (same renderer used for assistant
 * messages, so heading/list/link styling is consistent).
 */
@Component({
  selector: 'app-user-menu-link-modal',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MarkdownComponent, NgIcon],
  providers: [provideIcons({ heroXMark })],
  host: {
    class: 'block',
    '(keydown.escape)': 'onClose()',
  },
  template: `
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-500/75 dark:bg-gray-900/80"
      aria-hidden="true"
      (click)="onClose()"
    ></div>

    <div class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0">
      <div
        class="dialog-panel relative w-full transform overflow-hidden rounded-lg bg-white text-left shadow-xl sm:my-8 sm:max-w-2xl dark:bg-gray-800 dark:outline dark:-outline-offset-1 dark:outline-white/10"
        role="dialog"
        aria-modal="true"
        [attr.aria-labelledby]="titleId"
      >
        <div class="flex items-center justify-between border-b border-gray-200 px-6 py-4 dark:border-gray-700">
          <h3 [id]="titleId" class="text-base/7 font-semibold text-gray-900 dark:text-white">
            {{ data.label }}
          </h3>
          <button
            type="button"
            (click)="onClose()"
            class="rounded-md text-gray-400 hover:text-gray-500 focus:outline-2 focus:outline-offset-2 focus:outline-[var(--color-primary)] dark:hover:text-gray-300"
            aria-label="Close dialog"
          >
            <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
          </button>
        </div>

        <div class="max-h-[70vh] overflow-y-auto px-6 py-5">
          <div class="prose prose-sm max-w-none dark:prose-invert">
            <markdown [data]="data.bodyMarkdown" />
          </div>
        </div>

        <div class="flex justify-end border-t border-gray-200 px-6 py-3 dark:border-gray-700">
          <button
            type="button"
            (click)="onClose()"
            class="rounded-md bg-white px-3 py-2 text-sm/6 font-semibold text-gray-900 shadow-xs ring-1 ring-gray-300 ring-inset hover:bg-gray-50 dark:bg-white/10 dark:text-white dark:shadow-none dark:ring-white/5 dark:hover:bg-white/20"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  `,
  styles: `
    @import "tailwindcss";
    @custom-variant dark (&:where(.dark, .dark *));

    .dialog-backdrop {
      animation: backdrop-fade-in 200ms ease-out;
    }
    @keyframes backdrop-fade-in {
      from { opacity: 0; }
      to { opacity: 1; }
    }
    .dialog-panel {
      animation: dialog-fade-in-up 200ms ease-out;
    }
    @keyframes dialog-fade-in-up {
      from { opacity: 0; transform: translateY(1rem) scale(0.98); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }
  `,
})
export class UserMenuLinkModalComponent {
  private dialogRef = inject(DialogRef<void>);
  protected data = inject<UserMenuLinkModalData>(DIALOG_DATA);
  protected readonly titleId = `user-menu-link-modal-title-${crypto.randomUUID()}`;

  protected onClose(): void {
    this.dialogRef.close();
  }
}
