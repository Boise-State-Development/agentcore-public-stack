import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { Router } from '@angular/router';
import { CdkMenu, CdkMenuItem, CdkMenuTrigger } from '@angular/cdk/menu';
import { ConnectedPosition } from '@angular/cdk/overlay';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroBell } from '@ng-icons/heroicons/outline';
import { TooltipDirective } from '../tooltip/tooltip.directive';
import { NotificationsService } from '../../services/notifications/notifications.service';
import { AppNotification } from '../../services/notifications/notification.model';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { parseIso } from '../../utils/date';

/** Re-read on returning to the tab at most this often. */
const REFRESH_ON_FOCUS_MS = 60_000;

const ROLE_PHRASES: Record<string, string> = { editor: 'an editor', viewer: 'a viewer', owner: 'the owner' };

/** The sentence a notification reads as. Actor and project fall back to neutral words. */
export function describeNotification(n: AppNotification): string {
  const who = n.actorEmail || 'Someone';
  const project = n.projectName || 'a project';
  const role = ROLE_PHRASES[n.payload?.role ?? ''] ?? (n.payload?.role ? `a ${n.payload.role}` : null);
  switch (n.kind) {
    case 'project_invited':
      return role ? `${who} added you to ${project} as ${role}.` : `${who} added you to ${project}.`;
    case 'project_role_changed':
      return role ? `${who} made you ${role} in ${project}.` : `${who} changed your role in ${project}.`;
    case 'project_removed':
      return `${who} removed you from ${project}.`;
    case 'project_ownership_transferred':
      return `${who} made you the owner of ${project}.`;
    default:
      return 'You have a new notification.';
  }
}

/** Short relative time: "Just now", "5m", "3h", "2d", else a date. */
export function relativeTime(iso: string, now = Date.now()): string {
  const then = parseIso(iso).getTime();
  const mins = Math.floor((now - then) / 60_000);
  if (mins < 1) return 'Just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return parseIso(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

/**
 * The notification bell beside the user menu (shared-projects §6, PR-1.8c).
 *
 * The badge reads `unreadCount` from `GET /notifications`. The list loads with
 * the sidebar, again whenever the menu opens, and when the tab comes back into
 * view (at most once a minute) — there is no push channel, so that is how an
 * invitation sent while the tab sat in the background shows up.
 *
 * Opening a notification marks it read and, unless it says you were removed,
 * takes you to the project. The panel shows the newest 20; the inbox keeps 90
 * days, and older entries are not worth a pager in a menu.
 */
@Component({
  selector: 'app-notification-bell',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CdkMenuTrigger, CdkMenu, CdkMenuItem, NgIcon, TooltipDirective],
  providers: [provideIcons({ heroBell })],
  host: {
    class: 'block',
    '(document:visibilitychange)': 'onVisibilityChange()',
  },
  template: `
    <button
      type="button"
      [cdkMenuTriggerFor]="panel"
      [cdkMenuPosition]="positions"
      (cdkMenuOpened)="onOpened()"
      [appTooltip]="'Notifications'"
      appTooltipPosition="top"
      [attr.aria-label]="buttonLabel()"
      class="relative flex size-9 items-center justify-center rounded-2xl text-gray-500 transition-colors hover:bg-gray-200/60 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-white/5 dark:hover:text-white"
    >
      <ng-icon name="heroBell" class="size-5" aria-hidden="true" />
      @if (unreadCount() > 0) {
        <span
          class="absolute top-0.5 right-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-primary-accessible px-1 text-[10px]/4 font-semibold text-white ring-2 ring-gray-100 dark:ring-gray-900"
          aria-hidden="true"
        >{{ badge() }}</span>
      }
    </button>

    <ng-template #panel>
      <div
        cdkMenu
        class="notification-panel w-80 max-w-[calc(100vw-2rem)] overflow-hidden rounded-2xl bg-white shadow-lg ring-1 ring-black/5 focus:outline-hidden dark:bg-gray-800 dark:ring-white/10"
        aria-label="Notifications"
      >
        <p class="border-b border-gray-200 px-4 py-2.5 text-sm/6 font-semibold text-gray-900 dark:border-gray-700 dark:text-white">
          Notifications
        </p>

        <div class="max-h-96 overflow-y-auto py-1">
          @if (error() && notifications().length === 0) {
            <p class="px-4 py-6 text-center text-sm/6 text-gray-600 dark:text-gray-400">{{ error() }}</p>
          } @else if (!ready()) {
            <div class="px-4 py-3" aria-busy="true">
              <div class="h-10 animate-pulse rounded-xl bg-gray-100 dark:bg-gray-700"></div>
            </div>
          } @else if (notifications().length === 0) {
            <p class="px-4 py-6 text-center text-sm/6 text-gray-600 dark:text-gray-400">You’re all caught up.</p>
          } @else {
            @for (n of notifications(); track n.notificationId) {
              <button
                cdkMenuItem
                type="button"
                (cdkMenuItemTriggered)="open(n)"
                class="flex w-full items-start gap-2.5 px-4 py-2.5 text-left hover:bg-gray-50 focus:bg-gray-50 focus:outline-hidden dark:hover:bg-gray-700 dark:focus:bg-gray-700"
              >
                <span
                  class="mt-2 size-2 shrink-0 rounded-full"
                  [class]="n.readAt ? 'bg-transparent' : 'bg-primary-accessible dark:bg-primary-400'"
                  aria-hidden="true"
                ></span>
                <span class="min-w-0 flex-1">
                  <span class="block text-sm/5 text-gray-900 dark:text-white" [class.font-medium]="!n.readAt">
                    {{ describe(n) }}
                    @if (!n.readAt) {
                      <span class="sr-only">(unread)</span>
                    }
                  </span>
                  <span class="block text-xs/5 text-gray-600 dark:text-gray-300">{{ when(n) }}</span>
                </span>
              </button>
            }
          }
        </div>

        <!-- Below the list, so the menu's first item (focused on keyboard open) is
             the newest notification, inside the scroll region. -->
        @if (unreadCount() > 0) {
          <div class="border-t border-gray-200 px-2 py-1.5 dark:border-gray-700">
            <button
              cdkMenuItem
              type="button"
              (cdkMenuItemTriggered)="markAllRead()"
              class="w-full rounded-2xl px-2 py-1 text-center text-xs/5 font-medium text-primary-accessible hover:bg-gray-100 focus:bg-gray-100 focus:outline-hidden dark:text-primary-50 dark:hover:bg-gray-700 dark:focus:bg-gray-700"
            >
              Mark all as read
            </button>
          </div>
        }
      </div>
    </ng-template>
  `,
  styles: `
    .notification-panel {
      animation: notification-panel-in 150ms ease-out;
    }
    @keyframes notification-panel-in {
      from { opacity: 0; transform: translateY(0.25rem); }
      to { opacity: 1; transform: translateY(0); }
    }
    @media (prefers-reduced-motion: reduce) {
      .notification-panel { animation: none; }
    }
  `,
})
export class NotificationBellComponent {
  private service = inject(NotificationsService);
  private router = inject(Router);
  private sidenav = inject(SidenavService);

  protected readonly notifications = this.service.notifications;
  protected readonly unreadCount = this.service.unreadCount;
  protected readonly error = this.service.error;
  protected readonly ready = this.service.ready;

  protected readonly badge = computed(() => (this.unreadCount() > 9 ? '9+' : String(this.unreadCount())));
  protected readonly buttonLabel = computed(() => {
    const n = this.unreadCount();
    return n === 0 ? 'Notifications' : `Notifications, ${n} unread`;
  });

  /** Opens upward from the sidebar footer, like the user menu beside it. */
  protected readonly positions: ConnectedPosition[] = [
    { originX: 'end', originY: 'top', overlayX: 'start', overlayY: 'bottom', offsetX: 8, offsetY: -8 },
    { originX: 'start', originY: 'top', overlayX: 'start', overlayY: 'bottom', offsetY: -8 },
  ];

  private lastRefresh = 0;

  constructor() {
    void this.refresh();
  }

  protected describe(n: AppNotification): string {
    return describeNotification(n);
  }

  protected when(n: AppNotification): string {
    return relativeTime(n.createdAt);
  }

  protected onOpened(): void {
    void this.refresh();
  }

  protected onVisibilityChange(): void {
    if (document.visibilityState === 'visible' && Date.now() - this.lastRefresh > REFRESH_ON_FOCUS_MS) {
      void this.refresh();
    }
  }

  private refresh(): Promise<void> {
    this.lastRefresh = Date.now();
    return this.service.refresh();
  }

  protected markAllRead(): void {
    void this.service.markAllRead();
  }

  protected open(n: AppNotification): void {
    void this.service.markRead(n);
    if (n.kind !== 'project_removed' && n.projectId) {
      void this.router.navigate(['/projects', n.projectId]);
      this.sidenav.close();
    }
  }
}
