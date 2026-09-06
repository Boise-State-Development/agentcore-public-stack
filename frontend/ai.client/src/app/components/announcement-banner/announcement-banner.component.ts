import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroCheckCircle,
  heroExclamationTriangle,
  heroInformationCircle,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import { AnnouncementsService } from '../../services/announcements/announcements.service';
import {
  Announcement,
  AnnouncementSeverity,
} from '../../services/announcements/announcement.model';

/**
 * The ambient announcement surface (§D1) — a strip at the top of the shell.
 *
 * At most one is ever shown, and **the server picks which** (§D7: highest
 * severity, then oldest `publishAt`). There is no selection logic here; this
 * renders `bannerItem()` and nothing else.
 *
 * Three things about it are less obvious than they look.
 *
 * **It writes `seen` on render, once per announcement per tab.** That is what
 * clears the unread dot for someone who reads the banner and never opens the
 * panel. The write races the user's click on ✕, which is exactly why §D2 makes
 * the server-side rank monotonic — a late `seen` cannot clobber `dismissed`
 * and bring the banner back. Do not "fix" that race here by delaying or
 * ordering the writes; the guard belongs at the database.
 *
 * **Dismissal is durable, not client-side.** Unlike `quota-warning-banner`,
 * whose dismissal is a `localStorage` "not now" for a signal that recomputes
 * every turn, an announcement is one-shot: dismissing it means never again, on
 * every device (§D3). `AnnouncementsService.ack` still hides it locally when
 * the POST fails, so a transient 500 cannot trap a user under an
 * undismissable strip.
 *
 * **It overlays rather than occupying space.** The host is positioned
 * `absolute` inside the shell's `<main>`, so showing or dismissing it never
 * reflows the page — an earlier version was a flex child, and dismissing it
 * pulled the whole view up by its height. Overlaying also removes the reason
 * three pieces of viewport-fixed chrome used to offset against a measured
 * `--announcement-banner-height`: nothing has to move any more, so that
 * variable, its `ResizeObserver`, and all three offsets are gone.
 *
 * **It lives above the composer, not at the top of the shell.** A deviation
 * from §D1's "strip below the top nav", and a deliberate one: what these
 * announce — a new model, a new capability — is acted on in the composer, so
 * the notice belongs where the decision is made rather than in a corner the
 * eye has already left. It mounts from `chat-input` beside
 * `quota-warning-banner` for that reason, which also means it is a **chat
 * surface only**; What's New remains the everywhere-record, which is why
 * `panel` is forced onto every announcement server-side.
 *
 * **Which side of the composer it takes follows the composer.** In a
 * conversation the composer is pinned to the bottom of the viewport, so the
 * pill goes above it. On the empty state the composer is centred with the
 * greeting directly above it, so the pill goes below instead — otherwise it
 * floats over the greeting, which is exactly what it does at narrow widths.
 * The caller passes `placement`; it is derived from the container's
 * `isEmptyState()`, not measured, because that signal is what decides which
 * layout branch renders in the first place. Measuring viewport position would
 * re-derive the same fact less reliably, and would have to be recomputed on
 * resize, on scroll, and when the artifact pane opens.
 *
 * Either way it floats clear of the quota tabs, which stay visually attached
 * to the input.
 *
 * The wrapper is `pointer-events-none` and only the pill itself takes events,
 * so the full-width positioning strip cannot swallow clicks aimed at the
 * topnav or the sidebar buttons underneath it.
 *
 * Body markdown is deliberately *not* rendered here: this surface is one line.
 * The full body lives in What's New, which is why `panel` is forced onto every
 * announcement server-side.
 */
@Component({
  selector: 'app-announcement-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroCheckCircle,
      heroExclamationTriangle,
      heroInformationCircle,
      heroXMark,
    }),
  ],
  host: {
    // The positioning strip. `pointer-events-none` here (and `auto` on the
    // pill) keeps it from swallowing clicks meant for the chrome beneath.
    class:
      'pointer-events-none absolute inset-x-0 z-30 flex justify-center px-4',
    '[class.bottom-full]': "placement() === 'above'",
    '[class.mb-2]': "placement() === 'above'",
    '[class.top-full]': "placement() === 'below'",
    '[class.mt-2]': "placement() === 'below'",
  },
  template: `
    @if (announcement(); as item) {
      <div
        class="pointer-events-auto inline-flex max-w-full items-center gap-x-2 rounded-2xl border px-3 py-1.5 text-xs shadow-md"
        [class]="severityClass()"
        role="status"
        aria-live="polite"
      >
        <ng-icon
          [name]="iconName()"
          class="size-4 shrink-0"
          aria-hidden="true"
        />

        <p class="min-w-0 truncate font-medium">
          {{ bannerText() }}
        </p>

        @if (item.cta_url && item.cta_label) {
          <a
            [href]="item.cta_url"
            target="_blank"
            rel="noopener noreferrer"
            class="shrink-0 font-semibold underline underline-offset-2 hover:no-underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-current"
          >
            {{ item.cta_label }}
          </a>
        }

        <button
          type="button"
          (click)="onDismiss()"
          [attr.aria-label]="'Dismiss announcement: ' + item.title"
          class="-mr-1 flex size-5 shrink-0 items-center justify-center rounded-full hover:bg-black/10 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-current dark:hover:bg-white/10"
        >
          <ng-icon name="heroXMark" class="size-3.5" aria-hidden="true" />
        </button>
      </div>
    }
  `,
})
export class AnnouncementBannerComponent {
  private readonly announcements = inject(AnnouncementsService);

  /**
   * Which side of the composer to take. `'above'` suits a bottom-pinned
   * composer; `'below'` keeps the pill off the greeting when the composer is
   * centred on the empty state.
   */
  readonly placement = input<'above' | 'below'>('above');

  readonly announcement = computed<Announcement | null>(() =>
    this.announcements.bannerItem(),
  );

  /**
   * Announcements this tab has already reported as `seen`.
   *
   * Without it the POST repeats on every re-render of the strip. The write is
   * idempotent server-side, so this is a courtesy to the network rather than a
   * correctness guard.
   */
  private readonly reportedSeen = new Set<string>();

  /**
   * `summary` is the banner's line when the author wrote one — `title` can run
   * to 140 characters, which is a heading, not a strip.
   */
  readonly bannerText = computed<string>(() => {
    const item = this.announcement();
    if (!item) return '';
    return item.summary?.trim() || item.title;
  });

  readonly iconName = computed<string>(() => {
    switch (this.severity()) {
      case 'success':
        return 'heroCheckCircle';
      case 'warning':
        return 'heroExclamationTriangle';
      default:
        return 'heroInformationCircle';
    }
  });

  /**
   * Full literal class strings per severity, never concatenated.
   *
   * Tailwind scans source text for complete class names, so a built-up
   * `bg-state-${severity}-50` would compile to nothing and the strip would
   * render unstyled.
   */
  readonly severityClass = computed<string>(() => {
    switch (this.severity()) {
      case 'success':
        return 'border-state-success-200 bg-state-success-50 text-state-success-800 dark:border-state-success-800 dark:bg-state-success-900/30 dark:text-state-success-200';
      case 'warning':
        return 'border-state-warning-200 bg-state-warning-50 text-state-warning-800 dark:border-state-warning-800 dark:bg-state-warning-900/30 dark:text-state-warning-200';
      default:
        return 'border-state-info-200 bg-state-info-50 text-state-info-800 dark:border-state-info-800 dark:bg-state-info-900/30 dark:text-state-info-200';
    }
  });

  private readonly severity = computed<AnnouncementSeverity>(
    () => this.announcement()?.severity ?? 'info',
  );

  constructor() {
    effect(() => {
      const item = this.announcement();
      if (!item || this.reportedSeen.has(item.announcement_id)) return;
      this.reportedSeen.add(item.announcement_id);
      void this.announcements.ack(item.announcement_id, 'seen', 'banner');
    });
  }

  protected onDismiss(): void {
    const item = this.announcement();
    if (!item) return;
    void this.announcements.ack(item.announcement_id, 'dismissed', 'banner');
  }
}
