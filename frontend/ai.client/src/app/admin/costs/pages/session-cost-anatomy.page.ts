import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';
import { DatePipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowLeft, heroChevronDown } from '@ng-icons/heroicons/outline';
import { AdminCostHttpService } from '../services/admin-cost-http.service';
import { CacheStatus, SessionCallRow } from '../models';
import {
  AnatomyRow,
  FINGERPRINT_KEYS,
  FINGERPRINT_LABELS,
  FingerprintKey,
  buildAnatomyRows,
  truncateHash,
} from './session-cost-anatomy.util';

/**
 * Admin drill-down: per-model-call cost anatomy for one session.
 *
 * The forensic view for prompt-cache diagnostics — each call row carries its
 * cache status and prefix-fingerprint hashes, and the hash that flipped
 * between consecutive calls names the cache-buster (the diagnosis on
 * `miss_avoidable` and `partial_miss` rows).
 *
 * A `partial_miss` row is the one to read carefully: it *did* read from cache,
 * so it looks healthy at a glance, but the read is a leading segment (tools +
 * system) against a re-write of everything after it. Its Read column stays
 * flat turn after turn while Write tracks the whole conversation.
 */
@Component({
  selector: 'app-session-cost-anatomy',
  imports: [RouterLink, NgIcon, DatePipe],
  providers: [provideIcons({ heroArrowLeft, heroChevronDown })],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div>
      <!-- Back link -->
      <a
        routerLink="/admin/costs"
        class="mb-6 inline-flex items-center gap-2 rounded-2xl text-sm/6 font-medium text-gray-600 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-gray-400 dark:hover:text-white"
      >
        <ng-icon name="heroArrowLeft" class="size-4" aria-hidden="true" />
        Back to Cost Analytics
      </a>

      <!-- Page Header -->
      <div class="mb-6">
        <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">Session Cost Anatomy</h1>
        <p class="mt-1 truncate font-mono text-sm/6 text-gray-600 dark:text-gray-400" [title]="id()">
          {{ id() }}
        </p>
      </div>

      @if (anatomyResource.isLoading()) {
        <!-- Loading State -->
        <div class="flex h-64 items-center justify-center">
          <div class="flex flex-col items-center gap-4">
            <div
              class="size-12 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-gray-600 dark:border-t-blue-400"
            ></div>
            <p class="text-sm/6 text-gray-500 dark:text-gray-400">Loading session cost anatomy…</p>
          </div>
        </div>
      } @else if (notFound()) {
        <!-- 404: session has no cost rows -->
        <div
          class="rounded-2xl border border-dashed border-gray-300 bg-white p-12 text-center dark:border-gray-700 dark:bg-gray-800"
        >
          <p class="text-sm/6 font-medium text-gray-900 dark:text-white">No cost rows for this session</p>
          <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
            Nothing has been recorded under this session ID — check the ID, or the session may predate
            cost tracking.
          </p>
        </div>
      } @else if (anatomyResource.error()) {
        <!-- Error State -->
        <div
          class="rounded-2xl border border-red-200 bg-red-50 p-4 text-red-800 dark:border-red-800 dark:bg-red-900/20 dark:text-red-200"
        >
          <p class="text-sm/6">Failed to load session cost anatomy. Please try again.</p>
          <button
            type="button"
            (click)="anatomyResource.reload()"
            class="mt-2 text-sm/6 font-medium underline hover:no-underline"
          >
            Retry
          </button>
        </div>
      } @else if (anatomyResource.hasValue()) {
        <!-- Summary Header -->
        <div class="mb-6 grid grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-6">
          <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
            <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Total Cost
            </p>
            <p class="mt-1 text-lg/7 font-semibold text-gray-900 dark:text-white">
              {{ formatCurrency(anatomyResource.value().totalCost) }}
            </p>
          </div>
          <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
            <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Cache Efficiency
            </p>
            <p class="mt-1 text-lg/7 font-semibold text-gray-900 dark:text-white">
              {{ formatEfficiency(anatomyResource.value().cacheEfficiency) }}
            </p>
          </div>
          <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
            <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Avoidable Misses
            </p>
            <p
              class="mt-1 text-lg/7 font-semibold"
              [class]="
                unexplainedMisses() > 0
                  ? 'text-red-600 dark:text-red-400'
                  : 'text-gray-900 dark:text-white'
              "
            >
              {{ anatomyResource.value().avoidableMissCount }}
            </p>
            <!--
              #756 — an @-mention re-writes the prefix on purpose and looks exactly
              like the regression this page exists to find. The count stays whole (the
              spend was real); the explained part is named underneath so the red number
              above only means "unexplained".
            -->
            @if (anatomyResource.value().agentSwitchMissCount > 0) {
              <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                {{ anatomyResource.value().agentSwitchMissCount }} from an agent switch ·
                <span class="font-medium">{{ unexplainedMisses() }} unexplained</span>
              </p>
            }
          </div>
          <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
            <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Wasted
            </p>
            <p
              class="mt-1 text-lg/7 font-semibold"
              [class]="
                anatomyResource.value().wastedUsd > 0
                  ? 'text-red-600 dark:text-red-400'
                  : 'text-gray-900 dark:text-white'
              "
            >
              {{ formatCurrency(anatomyResource.value().wastedUsd, 4) }}
            </p>
            <!--
              Partial misses read from cache, so they used to be invisible here
              (reported as hits, $0 wasted) while costing as much as a full
              miss. Named under the total rather than in a tile of their own:
              the dollars are the headline, the shape is the explanation.
            -->
            @if (anatomyResource.value().partialMissCount > 0) {
              <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                {{ formatCurrency(anatomyResource.value().partialMissUsd, 4) }} from
                <span class="font-medium"
                  >{{ anatomyResource.value().partialMissCount }} partial
                  {{ anatomyResource.value().partialMissCount === 1 ? 'miss' : 'misses' }}</span
                >
              </p>
            }
          </div>
          <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
            <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Cache Read
            </p>
            <p class="mt-1 text-lg/7 font-semibold text-gray-900 dark:text-white">
              {{ formatTokens(anatomyResource.value().totalCacheReadTokens) }}
            </p>
          </div>
          <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
            <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Cache Write
            </p>
            <p class="mt-1 text-lg/7 font-semibold text-gray-900 dark:text-white">
              {{ formatTokens(anatomyResource.value().totalCacheWriteTokens) }}
            </p>
          </div>
        </div>

        <!-- Calls Table -->
        <div
          class="overflow-hidden rounded-2xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800"
        >
          <div class="overflow-x-auto">
            <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
              <thead>
                <tr class="text-left text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                  <th scope="col" class="px-3 py-3 sm:pl-4"><span class="sr-only">Expand</span></th>
                  <th scope="col" class="px-3 py-3">Time</th>
                  <th scope="col" class="px-3 py-3">Model</th>
                  <th scope="col" class="px-3 py-3 text-right">In</th>
                  <th scope="col" class="px-3 py-3 text-right">Read</th>
                  <th scope="col" class="px-3 py-3 text-right">Write</th>
                  <th scope="col" class="px-3 py-3 text-right">Out</th>
                  <th scope="col" class="px-3 py-3 text-right">Cost</th>
                  <th scope="col" class="px-3 py-3">Cache Status</th>
                  <th scope="col" class="px-3 py-3 text-right">Gap</th>
                  <th scope="col" class="px-3 py-3 text-right">Wasted</th>
                  <th scope="col" class="px-3 py-3">Fingerprints</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-gray-200 dark:divide-gray-700">
                @for (row of rows(); track row.index) {
                  <tr
                    class="text-sm/6 text-gray-700 dark:text-gray-300"
                    [class.bg-red-50]="row.call.cacheStatus === 'miss_avoidable'"
                    [class.dark:bg-red-900/10]="row.call.cacheStatus === 'miss_avoidable'"
                    [class.bg-orange-50]="row.call.cacheStatus === 'partial_miss'"
                    [class.dark:bg-orange-900/10]="row.call.cacheStatus === 'partial_miss'"
                  >
                    <td class="px-3 py-2 sm:pl-4">
                      <button
                        type="button"
                        (click)="toggleExpand(row.index)"
                        [attr.aria-expanded]="isExpanded(row.index)"
                        [attr.aria-controls]="'call-detail-' + row.index"
                        [attr.aria-label]="(isExpanded(row.index) ? 'Hide' : 'Show') + ' details for call ' + (row.index + 1)"
                        class="flex size-7 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                      >
                        <ng-icon
                          name="heroChevronDown"
                          class="size-4 transition-transform duration-150"
                          [class.rotate-180]="isExpanded(row.index)"
                          aria-hidden="true"
                        />
                      </button>
                    </td>
                    <td class="whitespace-nowrap px-3 py-2 tabular-nums" [title]="row.call.timestamp">
                      {{ row.call.timestamp | date: 'MMM d, HH:mm:ss' }}
                    </td>
                    <td class="max-w-48 truncate px-3 py-2 font-mono text-xs/5" [title]="row.call.modelId ?? ''">
                      {{ row.call.modelId ?? '—' }}
                    </td>
                    <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {{ formatTokens(row.call.inputTokens) }}
                    </td>
                    <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums text-green-700 dark:text-green-400">
                      {{ formatTokens(row.call.cacheReadTokens) }}
                    </td>
                    <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums text-blue-700 dark:text-blue-400">
                      {{ formatTokens(row.call.cacheWriteTokens) }}
                    </td>
                    <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {{ formatTokens(row.call.outputTokens) }}
                    </td>
                    <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {{ formatCurrency(row.call.cost, 4) }}
                    </td>
                    <td class="whitespace-nowrap px-3 py-2">
                      @if (row.call.cacheStatus; as status) {
                        <span [class]="getStatusClass(status)">{{ getStatusLabel(status) }}</span>
                      } @else {
                        <span class="text-gray-400 dark:text-gray-500">—</span>
                      }
                    </td>
                    <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {{ formatGap(row.call.cacheGapSeconds) }}
                      @if (row.call.cachePrefixGapSeconds; as prefixGap) {
                        <!-- The gap that decided the verdict, when a call with a
                             different prefix ran in between (e.g. an @-mention).
                             Without it, a miss_ttl_expired beside a short gap
                             reads as a bug rather than as the correct answer. -->
                        <span
                          class="ml-1 text-xs text-gray-500 dark:text-gray-400"
                          [title]="'Last call with the same prefix was ' + formatGap(prefixGap) + ' ago — that is the entry this call could have hit'"
                          >({{ formatGap(prefixGap) }})</span
                        >
                      }
                    </td>
                    <td
                      class="whitespace-nowrap px-3 py-2 text-right tabular-nums"
                      [class.text-red-600]="row.call.wastedUsd > 0"
                      [class.dark:text-red-400]="row.call.wastedUsd > 0"
                    >
                      {{ row.call.wastedUsd > 0 ? formatCurrency(row.call.wastedUsd, 4) : '—' }}
                    </td>
                    <td class="whitespace-nowrap px-3 py-2">
                      @if (row.call.prefixFingerprints; as fp) {
                        <div class="flex items-center gap-1.5">
                          @for (key of fingerprintKeys; track key) {
                            <span
                              [class]="getFingerprintClass(row, key)"
                              [title]="
                                fingerprintLabels[key] +
                                ': ' +
                                (fp[key] ?? 'not recorded') +
                                (isChanged(row, key) ? ' — changed since previous call' : '')
                              "
                            >
                              {{ fingerprintLabels[key] }} {{ truncateHash(fp[key]) }}
                            </span>
                          }
                        </div>
                      } @else {
                        <span class="text-gray-400 dark:text-gray-500">—</span>
                      }
                    </td>
                  </tr>
                  @if (isExpanded(row.index)) {
                    <tr [id]="'call-detail-' + row.index" class="bg-gray-50 dark:bg-gray-900/40">
                      <td colspan="12" class="px-4 py-3 sm:pl-14">
                        <dl class="grid grid-cols-1 gap-x-8 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
                          <div>
                            <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                              Timestamp
                            </dt>
                            <dd class="mt-0.5 font-mono text-xs/5 text-gray-700 dark:text-gray-300">
                              {{ row.call.timestamp }}
                            </dd>
                          </div>
                          <div>
                            <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                              Message ID
                            </dt>
                            <dd class="mt-0.5 font-mono text-xs/5 text-gray-700 dark:text-gray-300">
                              {{ row.call.messageId ?? '—' }}
                            </dd>
                          </div>
                          <div>
                            <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                              Message Count
                            </dt>
                            <dd class="mt-0.5 font-mono text-xs/5 text-gray-700 dark:text-gray-300">
                              {{ row.call.prefixFingerprints?.messageCount ?? '—' }}
                            </dd>
                          </div>
                          @for (key of fingerprintKeys; track key) {
                            <div>
                              <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                                {{ fingerprintLabels[key] }} Hash
                                @if (isChanged(row, key)) {
                                  <span class="ml-1 normal-case text-red-600 dark:text-red-400">(changed)</span>
                                }
                              </dt>
                              <dd
                                class="mt-0.5 break-all font-mono text-xs/5"
                                [class]="
                                  isChanged(row, key)
                                    ? 'text-red-600 dark:text-red-400'
                                    : 'text-gray-700 dark:text-gray-300'
                                "
                              >
                                {{ row.call.prefixFingerprints?.[key] ?? '—' }}
                              </dd>
                            </div>
                          }
                        </dl>
                      </td>
                    </tr>
                  }
                } @empty {
                  <tr>
                    <td colspan="12" class="px-4 py-8 text-center text-sm/6 text-gray-500 dark:text-gray-400">
                      No model calls recorded for this session.
                    </td>
                  </tr>
                }
              </tbody>
            </table>
          </div>
        </div>
      }
    </div>
  `,
})
export class SessionCostAnatomyPage {
  private costHttp = inject(AdminCostHttpService);

  /** Session ID from the `costs/sessions/:id` route (component input binding). */
  readonly id = input.required<string>();

  readonly fingerprintKeys = FINGERPRINT_KEYS;
  readonly fingerprintLabels = FINGERPRINT_LABELS;
  readonly truncateHash = truncateHash;

  readonly anatomyResource = resource({
    params: () => ({ id: this.id() }),
    loader: ({ params }) => firstValueFrom(this.costHttp.getSessionCostAnatomy(params.id)),
  });

  /** Chronological call rows annotated with fingerprint diffs. */
  readonly rows = computed<AnatomyRow[]>(() =>
    this.anatomyResource.hasValue() ? buildAnatomyRows(this.anatomyResource.value().calls) : []
  );

  /** True when the backend returned 404 — the session has no cost rows. */
  readonly notFound = computed(() => this.errorStatus(this.anatomyResource.error()) === 404);

  /**
   * Avoidable misses with no explanation — the number that should never move (#756).
   *
   * `avoidableMissCount` includes deliberate `@`-mention prefix swaps, which cost real
   * money but are not a regression. The backend reports the explained subset rather than
   * deducting it, so the page does the subtraction where a reader can see both halves.
   */
  readonly unexplainedMisses = computed(() => {
    const anatomy = this.anatomyResource.value();
    if (!anatomy) return 0;
    return Math.max(0, anatomy.avoidableMissCount - anatomy.agentSwitchMissCount);
  });

  private expandedRows = signal<ReadonlySet<number>>(new Set());

  isExpanded(index: number): boolean {
    return this.expandedRows().has(index);
  }

  toggleExpand(index: number): void {
    this.expandedRows.update((current) => {
      const next = new Set(current);
      if (next.has(index)) {
        next.delete(index);
      } else {
        next.add(index);
      }
      return next;
    });
  }

  isChanged(row: AnatomyRow, key: FingerprintKey): boolean {
    return row.changed.includes(key);
  }

  getFingerprintClass(row: AnatomyRow, key: FingerprintKey): string {
    const base = 'inline-flex items-center rounded-2xl px-2 py-0.5 font-mono text-xs/5';
    if (this.isChanged(row, key)) {
      return `${base} bg-red-100 font-semibold text-red-700 ring-1 ring-inset ring-red-300 dark:bg-red-900/40 dark:text-red-300 dark:ring-red-700`;
    }
    return `${base} bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400`;
  }

  getStatusClass(status: CacheStatus): string {
    const base = 'inline-flex items-center rounded-2xl px-2.5 py-0.5 text-xs/5 font-medium';
    switch (status) {
      case 'hit':
        return `${base} bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300`;
      case 'first_write':
        return `${base} bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300`;
      case 'miss_ttl_expired':
        return `${base} bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300`;
      case 'miss_avoidable':
        return `${base} bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300`;
      case 'partial_miss':
        return `${base} bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-300`;
      case 'uncached':
      default:
        return `${base} bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300`;
    }
  }

  getStatusLabel(status: CacheStatus): string {
    switch (status) {
      case 'hit':
        return 'Hit';
      case 'first_write':
        return 'First Write';
      case 'miss_ttl_expired':
        return 'Miss (TTL)';
      case 'miss_avoidable':
        return 'Miss (Avoidable)';
      case 'partial_miss':
        return 'Miss (Partial)';
      case 'uncached':
        return 'Uncached';
      default:
        return status;
    }
  }

  formatCurrency(value: number, maxFractionDigits = 2): string {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 2,
      maximumFractionDigits: maxFractionDigits,
    }).format(value);
  }

  formatTokens(tokens: number): string {
    if (tokens >= 1_000_000) {
      return `${(tokens / 1_000_000).toFixed(1)}M`;
    } else if (tokens >= 1_000) {
      return `${(tokens / 1_000).toFixed(1)}K`;
    }
    return tokens.toString();
  }

  formatEfficiency(efficiency: number | null): string {
    if (efficiency === null) {
      return '—';
    }
    return `${(efficiency * 100).toFixed(1)}%`;
  }

  formatGap(seconds: number | null | undefined): string {
    if (seconds == null) {
      return '—';
    }
    if (seconds >= 60) {
      const minutes = Math.floor(seconds / 60);
      const rest = seconds % 60;
      return rest > 0 ? `${minutes}m ${rest}s` : `${minutes}m`;
    }
    return `${seconds}s`;
  }

  private errorStatus(error: unknown): number | undefined {
    if (error instanceof HttpErrorResponse) {
      return error.status;
    }
    // resource() may wrap loader errors; check the cause chain.
    const cause = (error as { cause?: unknown } | null | undefined)?.cause;
    if (cause instanceof HttpErrorResponse) {
      return cause.status;
    }
    return undefined;
  }
}
