import { ChangeDetectionStrategy, Component, OnInit, inject } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { QuotaRequestsAdminService } from './services/quota-requests-admin.service';

interface QuotaRequestsTab {
  label: string;
  route: string;
  badge?: boolean;
}

/**
 * Quota requests and extensions (spec §6): Requests · Extensions · Settings.
 *
 * Its own area beside Quotas rather than tabs inside it, because the two are
 * different scopes: `/admin/quota` is `admin.quota` (the tier editor) and this
 * is `admin.quota_requests`, which exists so a department approver can be given
 * the queue without the tiers (§8.6). Tabs under `/admin/quota` would be
 * behind the wrong guard for exactly that person.
 */
@Component({
  selector: 'app-quota-requests-layout',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, RouterLinkActive, RouterOutlet],
  host: { class: 'block' },
  template: `
    <div class="mb-6">
      <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">Quota requests</h1>
      <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
        Decide requests for more quota, and manage the extensions and standing chargebacks granted on top of tiers.
      </p>
    </div>

    <div class="mb-6 border-b border-gray-200 dark:border-white/10">
      <nav class="-mb-px flex flex-wrap gap-x-6" aria-label="Quota request sections">
        @for (tab of tabs; track tab.route) {
          <a
            [routerLink]="tab.route"
            routerLinkActive="border-primary-500 text-primary-accessible dark:border-primary-400 dark:text-primary-accessible-dark"
            #rla="routerLinkActive"
            [attr.aria-current]="rla.isActive ? 'page' : null"
            class="inline-flex items-center gap-2 whitespace-nowrap border-b-2 border-transparent px-1 py-3 text-sm/6 font-medium text-gray-500 hover:border-gray-300 hover:text-gray-700 dark:text-gray-400 dark:hover:border-white/20 dark:hover:text-gray-200"
          >
            {{ tab.label }}
            @if (tab.badge && service.pendingCount() > 0) {
              <span
                class="inline-flex min-w-5 items-center justify-center rounded-full bg-primary-accessible px-1.5 text-xs/5 font-semibold text-white"
                [attr.aria-label]="service.pendingCount() + ' pending'"
              >{{ service.pendingCount() }}</span>
            }
          </a>
        }
      </nav>
    </div>

    <router-outlet />
  `,
})
export class QuotaRequestsLayout implements OnInit {
  protected readonly service = inject(QuotaRequestsAdminService);

  readonly tabs: QuotaRequestsTab[] = [
    { label: 'Requests', route: 'requests', badge: true },
    { label: 'Extensions', route: 'extensions' },
    { label: 'Settings', route: 'settings' },
  ];

  ngOnInit(): void {
    void this.service.refreshSummary();
  }
}
