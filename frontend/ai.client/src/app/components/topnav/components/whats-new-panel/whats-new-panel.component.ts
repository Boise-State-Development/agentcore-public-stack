import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
} from '@angular/core';
import { DialogRef } from '@angular/cdk/dialog';
import { MarkdownComponent } from 'ngx-markdown';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark, heroMegaphone } from '@ng-icons/heroicons/outline';
import { DialogDismissDirective } from '../../../dialog/dialog-dismiss.directive';
import { AnnouncementsService } from '../../../../services/announcements/announcements.service';
import { Announcement } from '../../../../services/announcements/announcement.model';
import { parseIso } from '../../../../utils/date';

/**
 * "What's New" — the durable announcement surface (§D1).
 *
 * Pull-based and uninterruptive by construction: it opens from the user menu,
 * lists everything this user is eligible for newest-first, and never appears
 * on its own. The banner (PR-4) and modal (PR-5) are the loud surfaces; this
 * one is the record, which is why `panel` is forced onto every announcement
 * server-side — dismissing a loud surface can never destroy the information.
 *
 * Markdown renders through `ngx-markdown` **with sanitization on**. Do not add
 * `[disableSanitizer]` here: `admin.announcements` is a delegable scope, so
 * this body may be authored by someone who is not a platform admin, and it is
 * broadcast to every user (§D10).
 *
 * Opening the panel marks everything in it `seen`, which is what clears the
 * unread dot.
 */
@Component({
  selector: 'app-whats-new-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogDismissDirective, MarkdownComponent, NgIcon],
  providers: [provideIcons({ heroXMark, heroMegaphone })],
  host: {
    class: 'block',
    '(keydown.escape)': 'onClose()',
  },
  template: `
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-900/40 dark:bg-gray-900/70"
      aria-hidden="true"
    ></div>

    <div
      class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0"
      appDialogDismiss
      (dismissed)="onClose()"
    >
      <div
        class="dialog-panel relative w-full overflow-hidden rounded-2xl border border-gray-200 bg-white text-left shadow-xl sm:my-8 sm:max-w-2xl dark:border-gray-700 dark:bg-gray-800"
        role="dialog"
        aria-modal="true"
        [attr.aria-labelledby]="titleId"
      >
        <div class="flex items-start justify-between gap-3 border-b border-gray-200 px-6 py-4 dark:border-gray-700">
          <h2 [id]="titleId" class="text-lg/7 font-semibold text-gray-900 dark:text-white">
            What's New
          </h2>
          <button
            type="button"
            (click)="onClose()"
            aria-label="Close What's New"
            class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
          >
            <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
          </button>
        </div>

        <div class="max-h-[70vh] overflow-y-auto px-6 py-5">
          @if (items().length === 0) {
            <div class="flex flex-col items-center gap-3 py-10 text-center">
              <ng-icon
                name="heroMegaphone"
                class="size-8 text-gray-300 dark:text-gray-600"
                aria-hidden="true"
              />
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                No announcements yet. New features will show up here.
              </p>
            </div>
          } @else {
            <ul class="divide-y divide-gray-200 dark:divide-gray-700">
              @for (item of items(); track item.announcement.announcement_id) {
                <li class="py-4 first:pt-0 last:pb-0">
                  <div class="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                    <h3 class="text-sm/6 font-semibold text-gray-900 dark:text-white">
                      {{ item.announcement.title }}
                    </h3>
                    @if (item.pill) {
                      <span
                        class="rounded-full bg-primary-600 px-2 py-0.5 text-xs/5 font-medium text-white"
                      >
                        {{ item.pill }}
                      </span>
                    }
                    <span class="ml-auto text-xs/5 text-gray-500 dark:text-gray-400">
                      {{ item.publishedLabel }}
                    </span>
                  </div>

                  <!-- message-block is the app's markdown stylesheet
                       (styles.css) — headings, lists, tables, code, links.
                       Reusing it is what makes an announcement body read like
                       an assistant message, per §D10. Note the prose classes
                       are NOT an option: the Tailwind typography plugin is
                       not installed, so they are inert and preflight strips
                       list markers. -->
                  <div class="message-block mt-2 text-sm/6 text-gray-700 dark:text-gray-300">
                    <markdown [data]="item.announcement.body_markdown" />
                  </div>

                  @if (item.announcement.cta_url && item.announcement.cta_label) {
                    <a
                      [href]="item.announcement.cta_url"
                      target="_blank"
                      rel="noopener noreferrer"
                      class="mt-2 inline-flex text-sm/6 font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark"
                    >
                      {{ item.announcement.cta_label }}
                    </a>
                  }
                </li>
              }
            </ul>
          }
        </div>

        <div class="flex justify-end border-t border-gray-200 px-6 py-3 dark:border-gray-700">
          <button
            type="button"
            (click)="onClose()"
            class="rounded-2xl bg-blue-600 px-3 py-2 text-sm/6 font-semibold text-white shadow-xs hover:bg-blue-500 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600"
          >
            Done
          </button>
        </div>
      </div>
    </div>
  `,
  styles: `
    @reference "../../../../../styles/theme.css";

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
export class WhatsNewPanelComponent {
  private readonly dialogRef = inject(DialogRef<void>);
  private readonly announcements = inject(AnnouncementsService);

  protected readonly titleId = `whats-new-title-${crypto.randomUUID()}`;

  /**
   * The unread state is snapshotted when the panel opens.
   *
   * `markPanelSeen()` clears it immediately, so reading `isUnread` live would
   * make every pill vanish the instant the dialog appeared — the user would
   * never see which entries were new to them. The dot on the avatar *should*
   * clear right away; the pills in the list should not.
   */
  private readonly unreadAtOpen = new Set(
    this.announcements
      .panelItems()
      .filter(a => this.announcements.isUnread(a))
      .map(a => a.announcement_id),
  );

  protected readonly items = computed(() =>
    this.announcements.panelItems().map(announcement => ({
      announcement,
      pill: this.pillFor(announcement),
      publishedLabel: this.formatPublished(announcement.publish_at),
    })),
  );

  constructor() {
    // Opening the panel is the acknowledgement that these were seen.
    void this.announcements.markPanelSeen();
  }

  protected onClose(): void {
    this.dialogRef.close();
  }

  /** "Updated" when the admin bumped the revision on something already read. */
  private pillFor(announcement: Announcement): string | null {
    if (!this.unreadAtOpen.has(announcement.announcement_id)) return null;
    return announcement.is_updated ? 'Updated' : 'New';
  }

  private formatPublished(publishAt: string): string {
    const date = parseIso(publishAt);
    if (Number.isNaN(date.getTime())) return '';

    const diffMs = Date.now() - date.getTime();
    const diffDays = Math.floor(diffMs / 86_400_000);
    if (diffDays < 1) return 'Today';
    if (diffDays === 1) return 'Yesterday';
    if (diffDays < 7) return `${diffDays} days ago`;
    return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  }
}
