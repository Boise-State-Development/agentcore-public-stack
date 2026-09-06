import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { DOCUMENT } from '@angular/common';
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
 * **It publishes its own height** as `--announcement-banner-height` on the
 * document root. The strip is a flex child of the shell's `<main>`, so the
 * scrolling content reflows on its own — but the chat topnav is
 * `position: fixed`, and would sit on top of the banner without that offset.
 * The value is measured rather than hardcoded because the line wraps on narrow
 * viewports.
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
  host: { class: 'block' },
  template: `
    @if (announcement(); as item) {
      <div
        class="flex items-center gap-x-3 border-b px-4 py-2.5 sm:px-6"
        [class]="severityClass()"
        role="status"
        aria-live="polite"
      >
        <ng-icon
          [name]="iconName()"
          class="size-5 shrink-0"
          aria-hidden="true"
        />

        <p class="min-w-0 flex-1 truncate text-sm/6 font-medium">
          {{ bannerText() }}
        </p>

        @if (item.cta_url && item.cta_label) {
          <a
            [href]="item.cta_url"
            target="_blank"
            rel="noopener noreferrer"
            class="shrink-0 text-sm/6 font-semibold underline underline-offset-2 hover:no-underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-current"
          >
            {{ item.cta_label }}
          </a>
        }

        <button
          type="button"
          (click)="onDismiss()"
          [attr.aria-label]="'Dismiss announcement: ' + item.title"
          class="-mr-1 flex size-7 shrink-0 items-center justify-center rounded-2xl hover:bg-black/10 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-current dark:hover:bg-white/10"
        >
          <ng-icon name="heroXMark" class="size-4" aria-hidden="true" />
        </button>
      </div>
    }
  `,
})
export class AnnouncementBannerComponent {
  private readonly announcements = inject(AnnouncementsService);
  private readonly host: ElementRef<HTMLElement> = inject(ElementRef);
  private readonly destroyRef = inject(DestroyRef);
  private readonly document = inject(DOCUMENT);

  /** The CSS custom property the shell's fixed chrome offsets against. */
  private static readonly HEIGHT_VAR = '--announcement-banner-height';

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

  /** Measured height of the strip, mirrored onto the document root. */
  private readonly height = signal(0);

  constructor() {
    effect(() => {
      const item = this.announcement();
      if (!item || this.reportedSeen.has(item.announcement_id)) return;
      this.reportedSeen.add(item.announcement_id);
      void this.announcements.ack(item.announcement_id, 'seen', 'banner');
    });

    effect(() => {
      const px = this.height();
      this.document.documentElement.style.setProperty(
        AnnouncementBannerComponent.HEIGHT_VAR,
        `${px}px`,
      );
    });

    // `ResizeObserver` rather than a one-shot measurement: the line wraps when
    // the viewport narrows or the artifact pane opens, and the fixed topnav
    // has to follow it down.
    if (typeof ResizeObserver !== 'undefined') {
      const observer = new ResizeObserver(entries => {
        const next = entries[0]?.contentRect.height ?? 0;
        this.height.set(Math.round(next));
      });
      observer.observe(this.host.nativeElement);
      this.destroyRef.onDestroy(() => observer.disconnect());
    }

    this.destroyRef.onDestroy(() => {
      this.document.documentElement.style.removeProperty(
        AnnouncementBannerComponent.HEIGHT_VAR,
      );
    });
  }

  protected onDismiss(): void {
    const item = this.announcement();
    if (!item) return;
    void this.announcements.ack(item.announcement_id, 'dismissed', 'banner');
  }
}
