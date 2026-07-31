import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowPath,
  heroClipboardDocumentList,
  heroExclamationTriangle,
} from '@ng-icons/heroicons/outline';

import { AuditService } from '../services/audit.service';
import {
  AuditRecord,
  actionLabel,
  fieldLabel,
  formatValue,
} from '../models/audit.model';

/**
 * Audit Log — who changed which role, when, and from what to what.
 *
 * `system_admin` only, and reachable only from this nav entry. The trail records
 * what admins do to roles *including* escalation attempts the write-through
 * guard refused, so the people it records must not be able to be granted the
 * ability to read it. The scope `admin.audit` exists so the route and nav are
 * covered by the same wiring test as every other admin page, and is marked
 * non-delegable on both ends.
 *
 * Three things the page has to make legible:
 *
 * - **A record is a diff, not a snapshot.** `before`/`after` carry only the
 *   fields that changed. That is why a row can say "Role updated · Admin access"
 *   rather than listing ten fields the form happened to post.
 * - **Denied attempts are the interesting rows.** They are the only records
 *   where nothing changed, and they are flagged rather than blended in.
 * - **Paging is by month.** The backing partition is month-sharded, so
 *   "load older" walks back a month at a time rather than scrolling one
 *   unbounded list. The month is shown so that is not mysterious.
 */
@Component({
  selector: 'app-audit-log',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroArrowPath,
      heroClipboardDocumentList,
      heroExclamationTriangle,
    }),
  ],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-4xl px-4 py-8 sm:px-6 lg:px-8">
        <div class="mb-6">
          <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">Audit Log</h1>
          <p class="mt-1 max-w-2xl text-sm/6 text-gray-600 dark:text-gray-400">
            Every change an admin has made to a role — what changed, who changed it, and
            what it was before. Attempts the permission guard refused are recorded too.
          </p>
          <p class="mt-2 max-w-2xl text-sm/6 text-gray-500 dark:text-gray-400">
            Records are kept for one year and cannot be edited or deleted.
          </p>
        </div>

        @if (error()) {
          <div
            role="alert"
            class="mb-4 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm/6 text-rose-800 dark:border-rose-900 dark:bg-rose-900/20 dark:text-rose-300"
          >
            {{ error() }}
          </div>
        }

        <!-- Month -->
        <div class="mb-6 flex flex-wrap items-center gap-3">
          <label for="audit-month" class="text-sm/6 font-medium text-gray-900 dark:text-white">
            Month
          </label>
          <input
            type="month"
            id="audit-month"
            [value]="month()"
            (change)="onMonthChange($event)"
            class="rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
          />
          <button
            type="button"
            (click)="reload()"
            [disabled]="loading()"
            class="inline-flex items-center gap-2 rounded-2xl border border-gray-300 px-3 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            <ng-icon name="heroArrowPath" class="size-5" aria-hidden="true" />
            Refresh
          </button>
        </div>

        @if (loading() && records().length === 0) {
          <div class="flex items-center justify-center py-16">
            <div
              class="size-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-gray-600 dark:border-t-blue-400"
            ></div>
            <span class="sr-only">Loading audit records</span>
          </div>
        } @else if (records().length === 0) {
          <div
            class="rounded-2xl border border-dashed border-gray-300 px-6 py-16 text-center dark:border-gray-600"
          >
            <ng-icon
              name="heroClipboardDocumentList"
              class="mx-auto size-8 text-gray-400 dark:text-gray-500"
              aria-hidden="true"
            />
            <h2 class="mt-3 text-sm/6 font-semibold text-gray-900 dark:text-white">
              Nothing recorded in {{ month() }}
            </h2>
            <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              Role changes made in this month would appear here. Pick another month to
              look further back.
            </p>
          </div>
        } @else {
          <ul class="space-y-3">
            @for (record of records(); track record.auditId) {
              <li
                class="rounded-2xl border bg-white dark:bg-gray-800"
                [class.border-gray-200]="record.outcome !== 'denied'"
                [class.dark:border-gray-700]="record.outcome !== 'denied'"
                [class.border-rose-300]="record.outcome === 'denied'"
                [class.dark:border-rose-800]="record.outcome === 'denied'"
              >
                <div class="p-4">
                  <div class="flex flex-wrap items-center gap-2">
                    <span class="text-sm/6 font-semibold text-gray-900 dark:text-white">
                      {{ label(record.action) }}
                    </span>
                    @if (record.outcome === 'denied') {
                      <span
                        class="inline-flex items-center gap-1 rounded-xs bg-rose-100 px-2 py-0.5 text-xs font-medium text-rose-800 dark:bg-rose-900/30 dark:text-rose-300"
                      >
                        <ng-icon name="heroExclamationTriangle" class="size-3" aria-hidden="true" />
                        Denied
                      </span>
                    }
                    <span class="text-sm/6 text-gray-500 dark:text-gray-400">
                      {{ record.targetId }}
                    </span>
                  </div>

                  <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
                    {{ record.actorEmail }} · {{ formatTimestamp(record.timestamp) }}
                  </p>

                  @if (record.reason) {
                    <p class="mt-2 text-sm/6 text-rose-700 dark:text-rose-300">
                      {{ record.reason }}
                    </p>
                  }

                  @if (record.changes.length > 0) {
                    <dl class="mt-3 space-y-2">
                      @for (field of record.changes; track field) {
                        <div class="text-sm/6">
                          <dt class="font-medium text-gray-700 dark:text-gray-300">
                            {{ fieldName(field) }}
                          </dt>
                          <dd class="text-gray-600 dark:text-gray-400">
                            <span class="line-through">{{ display(record.before[field]) }}</span>
                            <span aria-hidden="true"> → </span>
                            <span class="sr-only">changed to</span>
                            <span class="text-gray-900 dark:text-white">
                              {{ display(record.after[field]) }}
                            </span>
                          </dd>
                        </div>
                      }
                    </dl>
                  }
                </div>
              </li>
            }
          </ul>

          @if (nextCursor()) {
            <div class="mt-6 flex justify-center">
              <button
                type="button"
                (click)="loadMore()"
                [disabled]="loading()"
                class="rounded-2xl border border-gray-300 px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700"
              >
                {{ loading() ? 'Loading…' : 'Load more' }}
              </button>
            </div>
          } @else {
            <p class="mt-6 text-center text-sm/6 text-gray-500 dark:text-gray-400">
              End of {{ month() }}. Pick an earlier month to keep looking.
            </p>
          }
        }
      </div>
    </div>
  `,
})
export class AuditLogPage implements OnInit {
  private auditService = inject(AuditService);

  readonly records = signal<AuditRecord[]>([]);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly nextCursor = signal<string | null>(null);

  private readonly selectedMonth = signal<string>('');

  /**
   * Defaults to the current month. Computed rather than stamped at field
   * initialization so the page does not need a `new Date()` in a template
   * (unsupported) or a construction-time global.
   */
  readonly month = computed(() => this.selectedMonth() || currentMonth());

  ngOnInit(): void {
    void this.reload();
  }

  label = actionLabel;
  fieldName = fieldLabel;
  display = formatValue;

  formatTimestamp(value: string): string {
    // Older rows carry a `+00:00Z` spelling that `Date` rejects; normalizing
    // here keeps them from rendering as "Invalid Date".
    const parsed = new Date(value.replace('+00:00Z', 'Z'));
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
  }

  onMonthChange(event: Event): void {
    const value = (event.target as HTMLInputElement).value;
    if (!value) return;
    this.selectedMonth.set(value);
    void this.reload();
  }

  async reload(): Promise<void> {
    this.records.set([]);
    this.nextCursor.set(null);
    await this.fetch(null);
  }

  async loadMore(): Promise<void> {
    await this.fetch(this.nextCursor());
  }

  private async fetch(cursor: string | null): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const page = await this.auditService.fetchRecent({
        month: this.month(),
        cursor,
      });
      this.records.update(existing =>
        cursor ? [...existing, ...page.records] : page.records
      );
      this.nextCursor.set(page.nextCursor);
    } catch {
      this.error.set('Could not load the audit log. Try again in a moment.');
    } finally {
      this.loading.set(false);
    }
  }
}

function currentMonth(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
}
