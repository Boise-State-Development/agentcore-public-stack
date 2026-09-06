import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
} from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { MarkdownComponent } from 'ngx-markdown';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark } from '@ng-icons/heroicons/outline';
import { DialogDismissDirective } from '../dialog/dialog-dismiss.directive';
import { AnnouncementsService } from '../../services/announcements/announcements.service';
import {
  Announcement,
  AnnouncementSurface,
} from '../../services/announcements/announcement.model';

export interface AnnouncementModalData {
  announcement: Announcement;
  /**
   * Which surface the user came from, for ack attribution.
   *
   * Defaults to `'modal'` — the §D8 interruption, where the dialog *is* the
   * surface. The banner passes `'banner'` when the user clicks its text to
   * read the body, because the ack row's `surface` is the only record of what
   * actually drove the dismissal, and "the banner earned a read" is the one
   * number this interaction exists to produce. The funnel counters key on
   * action alone, so attribution never distorts them.
   */
  sourceSurface?: AnnouncementSurface;
}

/**
 * The interruptive announcement surface (§D1) — a dialog on next load.
 *
 * This is the only surface that can demand a real acknowledgement. When the
 * announcement carries `requiresAck`, **the confirm button is the only exit**:
 * the backdrop-dismiss directive's output is ignored, Escape is swallowed, and
 * the dialog is opened with CDK's `disableClose`. There is no ✕ and no
 * "Later". That is deliberate and it is also the reason `requiresAck` should
 * be rare — see the fatigue note in §11.
 *
 * Without `requiresAck` it behaves like any other dialog: ✕, Escape, backdrop
 * click and "Got it" all converge on the same `dismissed` ack.
 *
 * **Whether this opens at all is not decided here.** `AnnouncementModalService`
 * owns the §D8 turn-safety gate; by the time this component exists, the
 * decision to interrupt has already been made.
 *
 * Markdown renders through `ngx-markdown` **with sanitization on**. Do not add
 * `[disableSanitizer]`: `admin.announcements` is a delegable scope, so this
 * body may be authored by someone who is not a platform admin and it is
 * broadcast to every user (§D10). Body styling uses `.message-block`, the
 * app's real markdown stylesheet — the `prose` classes on the older
 * `user-menu-link-modal` are inert, because the Tailwind typography plugin is
 * not installed.
 */
@Component({
  selector: 'app-announcement-modal',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogDismissDirective, MarkdownComponent, NgIcon],
  providers: [provideIcons({ heroXMark })],
  host: {
    class: 'block',
    '(keydown.escape)': 'onEscape()',
  },
  template: `
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-900/40 dark:bg-gray-900/70"
      aria-hidden="true"
    ></div>

    <div
      class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0"
      appDialogDismiss
      (dismissed)="onBackdropDismiss()"
    >
      <div
        class="dialog-panel relative w-full overflow-hidden rounded-2xl border border-gray-200 bg-white text-left shadow-xl sm:my-8 sm:max-w-2xl dark:border-gray-700 dark:bg-gray-800"
        role="dialog"
        aria-modal="true"
        [attr.aria-labelledby]="titleId"
      >
        <div
          class="flex items-start justify-between gap-3 border-b border-gray-200 px-6 py-4 dark:border-gray-700"
        >
          <h2
            [id]="titleId"
            class="text-lg/7 font-semibold text-gray-900 dark:text-white"
          >
            {{ announcement.title }}
          </h2>

          @if (!requiresAck()) {
            <button
              type="button"
              (click)="onDismiss()"
              aria-label="Close announcement"
              class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
            >
              <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
            </button>
          }
        </div>

        <div class="max-h-[70vh] overflow-y-auto px-6 py-5">
          <div class="message-block text-sm/6 text-gray-700 dark:text-gray-300">
            <markdown [data]="announcement.body_markdown" />
          </div>

          @if (announcement.cta_url && announcement.cta_label) {
            <a
              [href]="announcement.cta_url"
              target="_blank"
              rel="noopener noreferrer"
              class="mt-4 inline-flex text-sm/6 font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark"
            >
              {{ announcement.cta_label }}
            </a>
          }
        </div>

        <div
          class="flex justify-end border-t border-gray-200 px-6 py-3 dark:border-gray-700"
        >
          <button
            type="button"
            (click)="onConfirm()"
            class="rounded-2xl bg-blue-600 px-3 py-2 text-sm/6 font-semibold text-white shadow-xs hover:bg-blue-500 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600"
          >
            {{ confirmLabel() }}
          </button>
        </div>
      </div>
    </div>
  `,
  styles: `
    @reference "../../../styles/theme.css";

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
export class AnnouncementModalComponent {
  private readonly dialogRef = inject(DialogRef<void>);
  private readonly data = inject<AnnouncementModalData>(DIALOG_DATA);
  private readonly announcements = inject(AnnouncementsService);

  protected readonly titleId = `announcement-modal-title-${crypto.randomUUID()}`;
  protected readonly announcement = this.data.announcement;

  /** Where every ack from this dialog is attributed. See `AnnouncementModalData`. */
  private readonly surface: AnnouncementSurface = this.data.sourceSurface ?? 'modal';

  protected readonly requiresAck = computed(
    () => this.announcement.requires_ack,
  );

  /**
   * "I understand" reads as a commitment; "Got it" reads as a dismissal. The
   * ack that gets recorded differs too, so the label should not lie about
   * which one the click writes.
   */
  protected readonly confirmLabel = computed(() =>
    this.requiresAck() ? 'I understand' : 'Got it',
  );

  constructor() {
    // Rendering is the `seen` (§D2). It is superseded moments later by the
    // `dismissed`/`acknowledged` the exit writes — but a user who reads this
    // and then closes the tab has still seen it, and the monotonic rank makes
    // the redundant write free.
    void this.announcements.ack(
      this.announcement.announcement_id,
      'seen',
      this.surface,
    );
  }

  /** The confirm button — the only exit when `requiresAck`. */
  protected onConfirm(): void {
    void this.announcements.ack(
      this.announcement.announcement_id,
      this.requiresAck() ? 'acknowledged' : 'dismissed',
      this.surface,
    );
    this.dialogRef.close();
  }

  protected onDismiss(): void {
    if (this.requiresAck()) return;
    void this.announcements.ack(
      this.announcement.announcement_id,
      'dismissed',
      this.surface,
    );
    this.dialogRef.close();
  }

  /**
   * Backdrop click and Escape are the same "not now" gesture, and both are
   * inert on a `requiresAck` announcement. CDK's `disableClose` already stops
   * Escape from closing the overlay; this guard exists so the ack is not
   * written either, and so the behaviour survives someone opening this dialog
   * without that option.
   */
  protected onBackdropDismiss(): void {
    this.onDismiss();
  }

  protected onEscape(): void {
    this.onDismiss();
  }
}
