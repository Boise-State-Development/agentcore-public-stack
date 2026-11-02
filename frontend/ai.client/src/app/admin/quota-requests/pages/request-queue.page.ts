import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { formatAmount, formatQuotaDate, perPeriod } from '../../../services/quota/quota-format';
import { AdminQuotaRequest, QuotaRequestStatus, toAmount } from '../models/quota-requests-admin.models';
import { QuotaRequestsAdminService, quotaAdminErrorMessage } from '../services/quota-requests-admin.service';
import { STATUS_LABELS, STATUS_PILL_CLASSES, age, describeFundingSource } from '../utils/quota-admin-copy';

type QueueFilter = 'open' | 'approved' | 'denied' | 'withdrawn';

const FILTERS: { value: QueueFilter; label: string }[] = [
  { value: 'open', label: 'Open' },
  { value: 'approved', label: 'Approved' },
  { value: 'denied', label: 'Denied' },
  { value: 'withdrawn', label: 'Withdrawn' },
];

/**
 * The queue (spec §6.2): pending first, then the ones waiting on the user,
 * oldest first within each; decided requests behind the filter.
 *
 * Usage here is what the person saw when they asked (the request's snapshot).
 * The detail page shows it live: the list route returns requests only, and a
 * live read per row would be a tier resolution and a cost read for each.
 */
@Component({
  selector: 'app-request-queue',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  host: { class: 'block' },
  template: `
    <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
      <div>
        <label for="queue-filter" class="sr-only">Show requests</label>
        <select
          id="queue-filter"
          [value]="filter()"
          (change)="setFilter($event)"
          class="rounded-2xl border border-gray-300 bg-white py-1.5 pl-3 pr-8 text-sm/6 text-gray-900 focus:border-primary-accessible focus:outline-none focus:ring-2 focus:ring-primary-accessible dark:border-gray-600 dark:bg-gray-900 dark:text-white"
        >
          @for (option of filters; track option.value) {
            <option [value]="option.value">{{ option.label }}</option>
          }
        </select>
      </div>
    </div>

    @if (error(); as message) {
      <p role="alert" class="mb-4 rounded-2xl bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">{{ message }}</p>
    }

    @if (loading() && !rows().length) {
      <p class="py-10 text-center text-sm/6 text-gray-600 dark:text-gray-400">Loading requests…</p>
    } @else if (!rows().length) {
      <p class="rounded-2xl border border-dashed border-gray-300 py-10 text-center text-sm/6 text-gray-600 dark:border-gray-600 dark:text-gray-400">
        {{ filter() === 'open' ? 'No requests are waiting.' : 'Nothing here.' }}
      </p>
    } @else {
      <div class="relative overflow-x-auto rounded-2xl border border-gray-200 dark:border-white/10">
        <table class="min-w-full divide-y divide-gray-200 text-left text-sm/6 dark:divide-white/10">
          <thead class="bg-gray-50 dark:bg-white/5">
            <tr>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Who</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Tier · usage when asked</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Asked for</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Through</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Why</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Funding</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Age</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Status</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-200 bg-white dark:divide-white/10 dark:bg-gray-900">
            @for (row of rows(); track row.requestId) {
              <tr class="cursor-pointer hover:bg-gray-50 dark:hover:bg-white/5" (click)="open(row)">
                <td class="px-4 py-3">
                  <a [routerLink]="row.requestId" (click)="$event.stopPropagation()"
                    class="font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark">
                    {{ row.userEmail }}
                  </a>
                </td>
                <td class="whitespace-nowrap px-4 py-3 text-gray-700 dark:text-gray-300">
                  {{ row.context.tierName || 'No tier' }} · {{ usage(row) }}
                  @if (row.context.blocked) {
                    <span class="ml-1 font-semibold text-state-danger-700 dark:text-state-danger-300">blocked</span>
                  }
                </td>
                <td class="whitespace-nowrap px-4 py-3 text-gray-900 dark:text-white">{{ askedFor(row) }}</td>
                <td class="whitespace-nowrap px-4 py-3 text-gray-700 dark:text-gray-300">{{ date(row.requestedUntil) }}</td>
                <td class="max-w-xs truncate px-4 py-3 text-gray-700 dark:text-gray-300" [title]="row.justification">{{ firstLine(row.justification) }}</td>
                <td class="max-w-[12rem] truncate px-4 py-3 text-gray-700 dark:text-gray-300">{{ funding(row) }}</td>
                <td class="whitespace-nowrap px-4 py-3 text-gray-700 dark:text-gray-300">{{ ageOf(row.createdAt) }}</td>
                <td class="whitespace-nowrap px-4 py-3">
                  <span class="rounded-full px-2 py-0.5 text-xs/5 font-medium" [class]="statusClass(row.status)">{{ statusLabel(row.status) }}</span>
                </td>
              </tr>
            }
          </tbody>
        </table>
      </div>
      @if (nextCursor()) {
        <div class="mt-4 flex justify-center">
          <button type="button" (click)="loadMore()" [disabled]="loading()"
            class="rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700">
            Load more
          </button>
        </div>
      }
    }
  `,
})
export class RequestQueuePage implements OnInit {
  private service = inject(QuotaRequestsAdminService);
  private router = inject(Router);

  readonly filters = FILTERS;
  readonly filter = signal<QueueFilter>('open');
  readonly rows = signal<AdminQuotaRequest[]>([]);
  readonly nextCursor = signal<string | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);

  ngOnInit(): void {
    void this.load();
  }

  setFilter(event: Event): void {
    this.filter.set((event.target as HTMLSelectElement).value as QueueFilter);
    void this.load();
  }

  async load(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    this.rows.set([]);
    this.nextCursor.set(null);
    try {
      const filter = this.filter();
      if (filter === 'open') {
        // Two partitions on the API; pending first, each oldest first (§6.2).
        const [pending, waiting] = await Promise.all([
          this.service.listRequests('pending'),
          this.service.listRequests('info_requested'),
        ]);
        this.rows.set([...pending.requests, ...waiting.requests]);
        void this.service.refreshSummary();
      } else {
        const page = await this.service.listRequests(filter);
        this.rows.set(page.requests);
        this.nextCursor.set(page.nextCursor);
      }
    } catch (error) {
      this.error.set(quotaAdminErrorMessage(error, 'Requests could not be loaded.'));
    } finally {
      this.loading.set(false);
    }
  }

  async loadMore(): Promise<void> {
    const filter = this.filter();
    const cursor = this.nextCursor();
    if (filter === 'open' || !cursor) return;
    this.loading.set(true);
    try {
      const page = await this.service.listRequests(filter, cursor);
      this.rows.update(rows => [...rows, ...page.requests]);
      this.nextCursor.set(page.nextCursor);
    } catch (error) {
      this.error.set(quotaAdminErrorMessage(error, 'More requests could not be loaded.'));
    } finally {
      this.loading.set(false);
    }
  }

  open(row: AdminQuotaRequest): void {
    void this.router.navigate(['/admin/quota-requests/requests', row.requestId]);
  }

  usage(row: AdminQuotaRequest): string {
    const used = toAmount(row.context.currentUsage);
    const limit = toAmount(row.context.effectiveLimit ?? row.context.baseLimit);
    const usedText = Number.isFinite(used) ? `$${used.toFixed(2)}` : '—';
    return Number.isFinite(limit) ? `${usedText} / ${formatAmount(limit)}` : `${usedText} / unlimited`;
  }

  askedFor(row: AdminQuotaRequest): string {
    const amount = formatAmount(toAmount(row.requestedAmount));
    const per = perPeriod(row.context.periodType);
    return row.kind === 'standing' ? `Standing, cap ${amount} ${per}` : `+${amount} ${per}`;
  }

  date(value: string): string {
    return formatQuotaDate(value);
  }

  firstLine(text: string): string {
    return text.split('\n')[0];
  }

  funding(row: AdminQuotaRequest): string {
    return describeFundingSource(row.fundingSource) || 'None';
  }

  ageOf(createdAt: string): string {
    return age(createdAt);
  }

  statusLabel(status: QuotaRequestStatus): string {
    return STATUS_LABELS[status];
  }

  statusClass(status: QuotaRequestStatus): string {
    return STATUS_PILL_CLASSES[status];
  }
}
