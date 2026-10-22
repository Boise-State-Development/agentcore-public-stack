import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { Router } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArchiveBox, heroPlus } from '@ng-icons/heroicons/outline';

/**
 * In place of the composer: this conversation's history was served from the
 * conversation archive because its Memory events expired. The agent restores
 * from Memory, so a new turn here would start with no context and overwrite
 * the archived first turn — the conversation is offered read-only, with a way
 * to start fresh. See docs/specs/conversation-search.md §10 q4.
 */
@Component({
  selector: 'app-expired-session-notice',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroArchiveBox, heroPlus })],
  // A container query, not a viewport one: the composer slot narrows with the
  // sidenav and artifact pane on a wide screen.
  host: { class: '@container block' },
  template: `
    <div
      role="status"
      class="flex flex-col gap-3 rounded-2xl border border-gray-200 bg-white px-4 py-3 shadow-xs @lg:flex-row @lg:items-center dark:border-gray-700 dark:bg-gray-800"
    >
      <div class="flex min-w-0 flex-1 items-start gap-2.5">
        <ng-icon name="heroArchiveBox" class="mt-0.5 size-5 shrink-0 text-gray-500 dark:text-gray-400" aria-hidden="true" />
        <div class="min-w-0 text-sm/6">
          <p class="font-medium text-gray-900 dark:text-white">This conversation is read-only</p>
          <p class="text-gray-600 dark:text-gray-400">
            It's older than its full history is kept. The text is saved so you can read and search it, but it can't be continued.
          </p>
        </div>
      </div>
      <button
        type="button"
        (click)="startNewConversation()"
        class="inline-flex shrink-0 items-center justify-center gap-1.5 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white shadow-xs transition hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
      >
        <ng-icon name="heroPlus" class="size-4" aria-hidden="true" />
        Start a new conversation
      </button>
    </div>
  `,
})
export class ExpiredSessionNoticeComponent {
  private router = inject(Router);

  protected startNewConversation(): void {
    this.router.navigate(['']);
  }
}
