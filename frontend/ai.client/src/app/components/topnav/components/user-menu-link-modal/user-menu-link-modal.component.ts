import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { MarkdownComponent } from 'ngx-markdown';
import { DialogShellComponent } from '../../../dialog/dialog-shell.component';

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
  imports: [DialogShellComponent, MarkdownComponent],
  host: { class: 'block' },
  template: `
    <app-dialog-shell [title]="data.label" size="lg" (closed)="onClose()">
      <div class="markdown-body prose prose-sm max-w-none dark:prose-invert">
        <markdown [data]="data.bodyMarkdown" />
      </div>

      <div dialogFooter class="flex justify-end border-t border-gray-200 px-6 py-3 dark:border-gray-700">
        <button
          type="button"
          (click)="onClose()"
          class="rounded-2xl bg-white px-3 py-2 text-sm/6 font-semibold text-gray-900 shadow-xs ring-1 ring-gray-300 ring-inset hover:bg-gray-50 dark:bg-white/10 dark:text-white dark:shadow-none dark:ring-white/5 dark:hover:bg-white/20"
        >
          Close
        </button>
      </div>
    </app-dialog-shell>
  `,
  styles: `
    @reference "../../../../../styles/theme.css";

    .markdown-body ::ng-deep a {
      color: var(--color-primary-500);
      text-decoration: underline;
      text-underline-offset: 2px;
    }
    .markdown-body ::ng-deep a:hover {
      color: var(--color-primary-700);
    }
    .markdown-body ::ng-deep a:focus-visible {
      outline: 2px solid var(--color-primary-500);
      outline-offset: 2px;
      border-radius: 0.125rem;
    }
    :host-context(.dark) .markdown-body ::ng-deep a {
      color: var(--color-primary-400);
    }
    :host-context(.dark) .markdown-body ::ng-deep a:hover {
      color: var(--color-primary-300);
    }
  `,
})
export class UserMenuLinkModalComponent {
  private dialogRef = inject(DialogRef<void>);
  protected data = inject<UserMenuLinkModalData>(DIALOG_DATA);

  protected onClose(): void {
    this.dialogRef.close();
  }
}
