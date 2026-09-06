import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroPencil,
  heroTrash,
  heroPlus,
  heroMegaphone,
  heroPaperAirplane,
  heroArchiveBox,
  heroArrowPath,
  heroWindow,
  heroRectangleGroup,
  heroBellAlert,
} from '@ng-icons/heroicons/outline';
import { AnnouncementsAdminService } from './services/announcements-admin.service';
import { Announcement, AnnouncementState } from './models/announcement.model';
import { parseIso } from '../../utils/date';

/**
 * Admin list of feature announcements.
 *
 * Mirrors `manage-user-menu-links`, plus the lifecycle actions an
 * announcement has and a link does not: publish, archive, and "Show again"
 * (the revision bump, §D4).
 */
@Component({
  selector: 'app-manage-announcements-page',
  imports: [RouterLink, NgIcon],
  providers: [
    provideIcons({
      heroPencil,
      heroTrash,
      heroPlus,
      heroMegaphone,
      heroPaperAirplane,
      heroArchiveBox,
      heroArrowPath,
      heroWindow,
      heroRectangleGroup,
      heroBellAlert,
    }),
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div>
      <div class="mb-8 flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 class="text-3xl/9 font-bold text-gray-900 dark:text-white">Announcements</h1>
          <p class="mt-1 text-gray-600 dark:text-gray-400">
            Tell users what changed. Everything published appears in What's New; a
            banner or modal additionally puts it in front of them.
          </p>
        </div>
        <a
          routerLink="/admin/manage-announcements/new"
          class="inline-flex items-center gap-2 rounded-sm bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus:outline-none focus:ring-2 focus:ring-primary-500"
        >
          <ng-icon name="heroPlus" class="size-5" />
          New announcement
        </a>
      </div>

      <!-- The norm this feature lives or dies by (§11). A control cannot
           enforce it, so say it where announcements are written. -->
      <div class="mb-6 rounded-sm border border-state-info-300 bg-state-info-50 p-4 text-sm/6 text-state-info-800 dark:border-state-info-700 dark:bg-state-info-900/20 dark:text-state-info-200">
        <strong class="font-semibold">Panel by default, banner when it matters, modal when it is a policy change.</strong>
        Every extra interruption costs attention on the next one. Users only ever
        see one banner and one modal at a time, no matter how many are eligible.
      </div>

      @if (loadError()) {
        <div class="mb-4 rounded-sm border border-state-danger-300 bg-state-danger-50 p-4 text-sm/6 text-state-danger-700 dark:border-state-danger-700 dark:bg-state-danger-900/20 dark:text-state-danger-300">
          Failed to load announcements. {{ loadError() }}
        </div>
      }

      @if (actionError()) {
        <div class="mb-4 rounded-sm border border-state-danger-300 bg-state-danger-50 p-4 text-sm/6 text-state-danger-700 dark:border-state-danger-700 dark:bg-state-danger-900/20 dark:text-state-danger-300">
          {{ actionError() }}
        </div>
      }

      @if (announcements().length === 0 && !isLoading()) {
        <div class="rounded-sm border border-gray-300 bg-white p-12 text-center dark:border-gray-600 dark:bg-gray-800">
          <ng-icon name="heroMegaphone" class="mx-auto size-8 text-gray-300 dark:text-gray-600" aria-hidden="true" />
          <p class="mt-3 text-base/7 text-gray-500 dark:text-gray-400">No announcements yet.</p>
          <a
            routerLink="/admin/manage-announcements/new"
            class="mt-4 inline-flex items-center gap-2 text-sm/6 font-medium text-primary-accessible hover:underline dark:text-primary-accessible-dark"
          >
            Write the first one →
          </a>
        </div>
      } @else {
        <div class="space-y-3">
          @for (item of rows(); track item.announcement.announcement_id) {
            <div class="rounded-sm border border-gray-300 bg-white p-4 dark:border-gray-600 dark:bg-gray-800">
              <div class="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                <div class="min-w-0 flex-1">
                  <div class="flex flex-wrap items-center gap-2">
                    <span class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">
                      {{ item.announcement.title }}
                    </span>

                    <span
                      class="shrink-0 rounded-sm px-2 py-0.5 text-xs/5 font-medium"
                      [class]="stateChipClass(item.announcement.state)"
                    >
                      {{ stateLabel(item.announcement.state) }}
                    </span>

                    @for (surface of item.announcement.surfaces; track surface) {
                      <span
                        class="inline-flex shrink-0 items-center gap-1 rounded-sm bg-gray-100 px-2 py-0.5 text-xs/5 font-medium text-gray-600 dark:bg-gray-700 dark:text-gray-300"
                        [title]="surfaceHint(surface)"
                      >
                        <ng-icon [name]="surfaceIcon(surface)" class="size-3.5" aria-hidden="true" />
                        {{ surface }}
                      </span>
                    }

                    @if (item.announcement.requires_ack) {
                      <span class="shrink-0 rounded-sm bg-state-warning-100 px-2 py-0.5 text-xs/5 font-medium text-state-warning-800 dark:bg-state-warning-900/40 dark:text-state-warning-300">
                        Requires acknowledgement
                      </span>
                    }

                    @if (item.announcement.revision > 1) {
                      <span
                        class="shrink-0 rounded-sm bg-gray-100 px-2 py-0.5 text-xs/5 font-medium text-gray-600 dark:bg-gray-700 dark:text-gray-300"
                        title="Each bump re-shows this to everyone who had dismissed it"
                      >
                        rev {{ item.announcement.revision }}
                      </span>
                    }
                  </div>

                  <p class="mt-1 truncate text-xs/5 text-gray-500 dark:text-gray-400">
                    {{ summarize(item.announcement.body_markdown) }}
                  </p>

                  <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                    {{ item.timing }}
                    @if (item.audience) {
                      · {{ item.audience }}
                    }
                  </p>

                  @if (item.reach; as reach) {
                    <p
                      class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400"
                      [title]="reachHint"
                    >
                      <span class="font-medium text-gray-700 dark:text-gray-300">Reach</span>
                      {{ reach }}
                    </p>
                  }
                </div>

                <div class="flex shrink-0 flex-wrap items-center gap-2">
                  @if (canPublish(item.announcement)) {
                    <button
                      type="button"
                      (click)="onPublish(item.announcement)"
                      [disabled]="busyId() !== null"
                      class="inline-flex items-center gap-1 rounded-sm border border-gray-300 bg-white px-2.5 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus:outline-none focus:ring-2 focus:ring-gray-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-500 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
                      [attr.aria-label]="'Publish ' + item.announcement.title"
                    >
                      <ng-icon name="heroPaperAirplane" class="size-4" />
                      Publish
                    </button>
                  }

                  @if (item.announcement.state === 'published') {
                    <button
                      type="button"
                      (click)="onRevise(item.announcement)"
                      [disabled]="busyId() !== null"
                      class="inline-flex items-center gap-1 rounded-sm border border-gray-300 bg-white px-2.5 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus:outline-none focus:ring-2 focus:ring-gray-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-500 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
                      title="Bump the revision so everyone sees this again"
                      [attr.aria-label]="'Show ' + item.announcement.title + ' again'"
                    >
                      <ng-icon name="heroArrowPath" class="size-4" />
                      Show again
                    </button>
                    <button
                      type="button"
                      (click)="onArchive(item.announcement)"
                      [disabled]="busyId() !== null"
                      class="inline-flex items-center gap-1 rounded-sm border border-gray-300 bg-white px-2.5 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus:outline-none focus:ring-2 focus:ring-gray-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-500 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
                      [attr.aria-label]="'Archive ' + item.announcement.title"
                    >
                      <ng-icon name="heroArchiveBox" class="size-4" />
                      Archive
                    </button>
                  }

                  <a
                    [routerLink]="['/admin/manage-announcements/edit', item.announcement.announcement_id]"
                    class="inline-flex items-center gap-1 rounded-sm border border-gray-300 bg-white px-2.5 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus:outline-none focus:ring-2 focus:ring-gray-500 dark:border-gray-500 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
                    [attr.aria-label]="'Edit ' + item.announcement.title"
                  >
                    <ng-icon name="heroPencil" class="size-4" />
                    <span class="sr-only sm:not-sr-only">Edit</span>
                  </a>

                  <button
                    type="button"
                    (click)="onDelete(item.announcement)"
                    [disabled]="busyId() !== null"
                    class="inline-flex items-center gap-1 rounded-sm border border-state-danger-300 bg-white px-2.5 py-1.5 text-sm/6 font-medium text-state-danger-700 hover:bg-state-danger-50 focus:outline-none focus:ring-2 focus:ring-state-danger-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-state-danger-500 dark:bg-gray-700 dark:text-state-danger-400 dark:hover:bg-state-danger-900/20"
                    [attr.aria-label]="'Delete ' + item.announcement.title"
                  >
                    <ng-icon name="heroTrash" class="size-4" />
                    <span class="sr-only sm:not-sr-only">Delete</span>
                  </button>
                </div>
              </div>
            </div>
          }
        </div>
      }
    </div>
  `,
})
export class ManageAnnouncementsPage {
  private readonly service = inject(AnnouncementsAdminService);

  constructor() {
    this.service.ensureLoaded();

    // Reach is a second endpoint per announcement, so it is fetched once the
    // list resolves rather than blocking it. `loadStats` skips ids it has
    // already requested, so re-running on every list change is cheap and the
    // effect cannot feed itself.
    effect(() => {
      const announcements = this.announcements();
      if (announcements.length === 0) return;
      void this.service.loadStats(announcements);
    });
  }

  protected readonly announcements = this.service.announcements;
  protected readonly isLoading = computed(() =>
    this.service.announcementsResource.isLoading(),
  );
  protected readonly loadError = computed(() => {
    const err = this.service.announcementsResource.error();
    if (!err) return null;
    return err instanceof Error ? err.message : String(err);
  });

  protected readonly busyId = signal<string | null>(null);
  protected readonly actionError = signal<string | null>(null);

  protected readonly rows = computed(() =>
    this.announcements().map(announcement => ({
      announcement,
      timing: this.describeTiming(announcement),
      audience: this.describeAudience(announcement),
      reach: this.describeReach(announcement),
    })),
  );

  protected readonly reachHint =
    'Approximate. Counts are cumulative — anyone who acknowledged also ' +
    'counts as dismissed and as seen. The audience size is an estimate that ' +
    'moves as people join.';

  protected canPublish(a: Announcement): boolean {
    // Archived is terminal; the server refuses to publish out of it, so do not
    // offer a button that returns a 400.
    return a.state === 'draft' || a.state === 'scheduled';
  }

  protected stateLabel(state: AnnouncementState): string {
    return state.charAt(0).toUpperCase() + state.slice(1);
  }

  protected stateChipClass(state: AnnouncementState): string {
    switch (state) {
      case 'published':
        return 'bg-state-success-100 text-state-success-800 dark:bg-state-success-900/40 dark:text-state-success-300';
      case 'scheduled':
        return 'bg-state-info-100 text-state-info-700 dark:bg-state-info-900/40 dark:text-state-info-300';
      case 'archived':
        return 'bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400';
      default:
        return 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300';
    }
  }

  protected surfaceIcon(surface: string): string {
    if (surface === 'banner') return 'heroRectangleGroup';
    if (surface === 'modal') return 'heroBellAlert';
    return 'heroWindow';
  }

  protected surfaceHint(surface: string): string {
    if (surface === 'banner')
      return 'A pill above the chat composer, in the chat view only. At most one at a time.';
    if (surface === 'modal') return 'A dialog on next load. At most one at a time.';
    return "Always on. The What's New entry in the user menu.";
  }

  protected summarize(markdown: string | null | undefined): string {
    if (!markdown) return '(empty)';
    const stripped = markdown.replace(/[#*_`>\-]/g, '').replace(/\s+/g, ' ').trim();
    return stripped.length > 140 ? stripped.slice(0, 140) + '…' : stripped;
  }

  /**
   * One line of reach, or null when there is nothing honest to say.
   *
   * Null for a draft (nothing has been shown, so a row of zeroes would read
   * as "nobody engaged" rather than "not sent yet") and while the fetch is
   * still in flight.
   *
   * The counts are a funnel, not a partition — see `AnnouncementStats`. They
   * are rendered as such: "12 seen · 8 dismissed" means 8 of those 12, not 20
   * people.
   */
  private describeReach(a: Announcement): string | null {
    if (!AnnouncementsAdminService.hasReach(a)) return null;
    const stats = this.service.statsFor(a.announcement_id);
    if (!stats) return null;

    const parts = [`${stats.seen} seen`, `${stats.dismissed} dismissed`];
    // Only meaningful where an acknowledgement was ever asked for.
    if (a.requires_ack) parts.push(`${stats.acknowledged} acknowledged`);

    const line = parts.join(' · ');
    return stats.targeted != null
      ? `${line} — of ~${stats.targeted} targeted (estimate)`
      : `${line} (audience not estimated)`;
  }

  private describeTiming(a: Announcement): string {
    const published = this.formatDate(a.publish_at);
    const verb = a.state === 'published' ? 'Live since' : 'Publishes';
    const base = `${verb} ${published}`;
    return a.expires_at ? `${base} · expires ${this.formatDate(a.expires_at)}` : base;
  }

  private describeAudience(a: Announcement): string | null {
    const roles = a.target_roles ?? [];
    const audience = roles.includes('*') || roles.length === 0
      ? 'Everyone'
      : roles.join(', ');
    return a.show_to_new_users
      ? `${audience} (including users who join later)`
      : audience;
  }

  private formatDate(value: string | null | undefined): string {
    if (!value) return '—';
    const date = parseIso(value);
    if (Number.isNaN(date.getTime())) return '—';
    return date.toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    });
  }

  protected async onPublish(a: Announcement): Promise<void> {
    await this.run(a, () => this.service.publish(a.announcement_id));
  }

  protected async onArchive(a: Announcement): Promise<void> {
    if (!confirm(`Archive "${a.title}"? It stops showing, but acknowledgements are kept.`)) return;
    await this.run(a, () => this.service.archive(a.announcement_id));
  }

  protected async onRevise(a: Announcement): Promise<void> {
    // Worth a confirm: this re-surfaces the announcement for everyone who had
    // already dismissed it, which is exactly what an admin fixing a typo does
    // NOT want (that is what Edit is for).
    if (
      !confirm(
        `Show "${a.title}" again?\n\nThis bumps the revision, so everyone who dismissed it will see it once more. Editing the text does not do this.`,
      )
    ) {
      return;
    }
    await this.run(a, () => this.service.revise(a.announcement_id));
  }

  protected async onDelete(a: Announcement): Promise<void> {
    if (!confirm(`Delete "${a.title}"? This cannot be undone. Archive instead to keep the record.`)) return;
    await this.run(a, () => this.service.remove(a.announcement_id));
  }

  private async run(a: Announcement, action: () => Promise<unknown>): Promise<void> {
    this.busyId.set(a.announcement_id);
    this.actionError.set(null);
    try {
      await action();
    } catch (err: unknown) {
      const detail =
        (err as { error?: { detail?: string }; message?: string })?.error?.detail ??
        (err as Error)?.message ??
        'The action failed. Please try again.';
      this.actionError.set(detail);
    } finally {
      this.busyId.set(null);
    }
  }
}
