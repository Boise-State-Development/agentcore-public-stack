import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { ConfirmationDialogComponent, ConfirmationDialogData } from '../../../components/confirmation-dialog';
import { formatQuotaDate, formatUsd } from '../../../services/quota/quota-format';
import {
  ChargebackFund,
  ChargebackModeFilter,
  ChargebackPeriod,
  ChargebackReport,
  DecimalString,
  FundingField,
  QuotaChargeLine,
  toAmount,
} from '../models/quota-requests-admin.models';
import { QuotaRequestsAdminService, quotaAdminErrorMessage } from '../services/quota-requests-admin.service';
import {
  ChargebackRangeKind,
  closesAt,
  formatPeriod,
  lastCompleteMonth,
  monthOf,
  rangeFor,
} from '../utils/chargeback-periods';
import { describeFundingSource } from '../utils/quota-admin-copy';

type Grouping = 'fund' | 'group';

interface ChargeGroup {
  key: string;
  title: string;
  detail: string;
  lines: QuotaChargeLine[];
  consumed: number;
  /** null when any line in the group is uncapped. */
  granted: number | null;
}

const CENTRAL = 'central';

/**
 * The chargeback report (spec §6.6, §9): per funding source, what extensions
 * were granted and what they actually consumed, for a month, a quarter or a
 * fiscal year.
 *
 * The current month, and a past one inside its 48-hour grace window, is
 * computed on every read and labelled Provisional. A past month closes on its
 * first read after that and never moves again unless finance recalculates it,
 * which is audited with each fund's total before and after.
 */
@Component({
  selector: 'app-chargeback-report',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  host: { class: 'block' },
  template: `
    <div class="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div class="flex flex-wrap items-end gap-4">
        <div>
          <label for="cb-range" class="block text-xs/5 font-medium text-gray-700 dark:text-gray-300">Range</label>
          <select id="cb-range" [value]="rangeKind()" (change)="setRangeKind($event)" [class]="selectClass">
            <option value="month">One month</option>
            <option value="quarter">Quarter</option>
            <option value="fiscal_year">Fiscal year</option>
          </select>
        </div>
        <div>
          <label for="cb-month" class="block text-xs/5 font-medium text-gray-700 dark:text-gray-300">{{ rangeKind() === 'month' ? 'Month' : 'Including' }}</label>
          <input id="cb-month" type="month" [value]="anchor()" [max]="currentMonth" (change)="setAnchor($event)"
            class="mt-1 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 text-gray-900 focus:border-primary-accessible focus:outline-none focus:ring-2 focus:ring-primary-accessible dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:[color-scheme:dark]" />
        </div>
        <div>
          <label for="cb-fund" class="block text-xs/5 font-medium text-gray-700 dark:text-gray-300">Funding source</label>
          <select id="cb-fund" [value]="fundFilter()" (change)="setFund($event)" [class]="selectClass + ' max-w-[16rem]'">
            <option value="">All</option>
            <option value="central">Central budget</option>
            @for (fund of funds(); track fund.fundingKey) {
              <option [value]="fund.fundingKey">{{ fundName(fund.label, fund.fields) }}</option>
            }
          </select>
        </div>
        <div>
          <label for="cb-mode" class="block text-xs/5 font-medium text-gray-700 dark:text-gray-300">Mode</label>
          <select id="cb-mode" [value]="modeFilter()" (change)="setMode($event)" [class]="selectClass">
            <option value="">All</option>
            <option value="central">Central</option>
            <option value="chargeback">Charge back</option>
            <option value="standing">Standing</option>
          </select>
        </div>
        @if (groupFieldLabel(); as groupLabel) {
          <div>
            <label for="cb-group" class="block text-xs/5 font-medium text-gray-700 dark:text-gray-300">Group by</label>
            <select id="cb-group" [value]="grouping()" (change)="setGrouping($event)" [class]="selectClass">
              <option value="fund">Funding source</option>
              <option value="group">{{ groupLabel }}</option>
            </select>
          </div>
        }
      </div>
      <button type="button" (click)="exportCsv()" [disabled]="exporting()"
        class="rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-60 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700">
        {{ exporting() ? 'Exporting…' : 'Export CSV' }}
      </button>
    </div>

    @if (error(); as message) {
      <p role="alert" class="mb-4 rounded-2xl bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">{{ message }}</p>
    }
    @if (notice(); as message) {
      <p role="status" class="mb-4 rounded-2xl bg-state-success-50 px-4 py-3 text-sm/6 text-state-success-900 dark:bg-state-success-900/20 dark:text-state-success-200">{{ message }}</p>
    }

    @if (report(); as r) {
      <ul class="mb-4 flex flex-wrap gap-2" aria-label="Months in this report">
        @for (p of r.periods; track p.period) {
          <li class="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-2xl border border-gray-200 px-3 py-1.5 text-sm/6 dark:border-white/10">
            <span class="font-medium text-gray-900 dark:text-white">{{ periodName(p.period) }}</span>
            @if (p.status === 'closed') {
              <span class="rounded-full bg-gray-100 px-2 py-0.5 text-xs/5 font-medium text-gray-800 dark:bg-white/10 dark:text-gray-200">Closed</span>
              <span class="text-xs/5 text-gray-600 dark:text-gray-400">{{ closedText(p) }}</span>
              <button type="button" (click)="recalculate(p)" [disabled]="recalculating() === p.period"
                [attr.aria-label]="'Recalculate ' + periodName(p.period)"
                class="rounded-2xl px-2 py-0.5 text-xs/5 font-medium text-primary-accessible hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-60 dark:text-primary-accessible-dark dark:hover:bg-white/10">
                {{ recalculating() === p.period ? 'Recalculating…' : 'Recalculate' }}
              </button>
            } @else {
              <span class="rounded-full bg-state-warning-100 px-2 py-0.5 text-xs/5 font-medium text-state-warning-900 dark:bg-state-warning-900/30 dark:text-state-warning-200">Provisional</span>
              <span class="text-xs/5 text-gray-600 dark:text-gray-400">{{ provisionalText(p) }}</span>
            }
          </li>
        }
      </ul>

      <dl class="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div class="rounded-2xl border border-gray-200 px-4 py-3 dark:border-white/10">
          <dt class="text-xs/5 font-medium text-gray-600 dark:text-gray-400">Central budget</dt>
          <dd class="text-lg/7 font-semibold text-gray-900 dark:text-white">{{ usd(r.totals.central) }}</dd>
        </div>
        <div class="rounded-2xl border border-gray-200 px-4 py-3 dark:border-white/10">
          <dt class="text-xs/5 font-medium text-gray-600 dark:text-gray-400">Charged to funding sources</dt>
          <dd class="text-lg/7 font-semibold text-gray-900 dark:text-white">{{ usd(r.totals.chargeback) }}</dd>
        </div>
        <div class="rounded-2xl border border-gray-200 px-4 py-3 dark:border-white/10">
          <dt class="text-xs/5 font-medium text-gray-600 dark:text-gray-400">Over the limit, not covered</dt>
          <dd class="text-lg/7 font-semibold text-gray-900 dark:text-white">{{ usd(r.totals.unattributed) }}</dd>
        </div>
      </dl>
    }

    @if (loading() && !report()) {
      <p class="py-10 text-center text-sm/6 text-gray-600 dark:text-gray-400">Loading the report…</p>
    } @else if (report() && !groups().length) {
      <p class="rounded-2xl border border-dashed border-gray-300 py-10 text-center text-sm/6 text-gray-600 dark:border-gray-600 dark:text-gray-400">No extensions were active in this range.</p>
    } @else if (groups().length) {
      <div class="relative overflow-x-auto rounded-2xl border border-gray-200 dark:border-white/10" [attr.aria-busy]="loading()">
        <table class="min-w-full divide-y divide-gray-200 text-left text-sm/6 dark:divide-white/10">
          <caption class="sr-only">Chargeback report, grouped by {{ grouping() === 'fund' ? 'funding source' : groupFieldLabel() }}</caption>
          <thead class="bg-gray-50 dark:bg-white/5">
            <tr>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Extension</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">User</th>
              @if (multiPeriod()) {
                <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Month</th>
              }
              <th scope="col" class="px-4 py-2 text-right font-semibold text-gray-900 dark:text-white">Granted</th>
              <th scope="col" class="px-4 py-2 text-right font-semibold text-gray-900 dark:text-white">Consumed</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Mode</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Approved by</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Approved</th>
            </tr>
          </thead>
          @for (g of groups(); track g.key) {
            <tbody class="divide-y divide-gray-200 bg-white dark:divide-white/10 dark:bg-gray-900">
              <tr class="bg-gray-50/60 dark:bg-white/[0.03]">
                <th scope="colgroup" [attr.colspan]="multiPeriod() ? 3 : 2" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">
                  <button type="button" (click)="toggle(g.key)" [attr.aria-expanded]="isOpen(g.key)"
                    class="flex items-start gap-2 rounded-lg text-left focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500">
                    <span aria-hidden="true" class="mt-0.5 inline-block w-3 text-gray-500 dark:text-gray-400">{{ isOpen(g.key) ? '▾' : '▸' }}</span>
                    <span>
                      {{ g.title }}
                      <span class="block text-xs/5 font-normal text-gray-600 dark:text-gray-400">{{ g.detail }}</span>
                    </span>
                  </button>
                </th>
                <td class="whitespace-nowrap px-4 py-2 text-right font-medium text-gray-700 dark:text-gray-300">{{ g.granted === null ? 'Uncapped' : money(g.granted) }}</td>
                <td class="whitespace-nowrap px-4 py-2 text-right font-semibold text-gray-900 dark:text-white">{{ money(g.consumed) }}</td>
                <td colspan="3"></td>
              </tr>
              @if (isOpen(g.key)) {
                @for (l of g.lines; track l.period + l.extensionId) {
                  <tr>
                    <td class="px-4 py-3 pl-9 text-gray-900 dark:text-white">
                      @if (l.requestId) {
                        <a [routerLink]="['/admin/quota-requests/requests', l.requestId]" class="font-mono text-xs/5 text-primary-accessible hover:underline dark:text-primary-accessible-dark" [title]="l.extensionId">{{ shortId(l.extensionId) }}</a>
                      } @else {
                        <span class="font-mono text-xs/5" [title]="l.extensionId">{{ shortId(l.extensionId) }}</span>
                      }
                      @if (l.revokedAt) {
                        <span class="ml-1 rounded-full bg-gray-100 px-2 py-0.5 text-xs/5 font-medium text-gray-800 dark:bg-white/10 dark:text-gray-200" [title]="'Revoked ' + date(l.revokedAt)">Revoked</span>
                      }
                      @if (l.baseLimitAmbiguous) {
                        <span class="mt-1 block text-xs/5 text-state-warning-800 dark:text-state-warning-200">
                          ⚠ Base limit changed during period; the larger ({{ usd(l.baseLimit) }}) was used
                        </span>
                      }
                    </td>
                    <td class="px-4 py-3 text-gray-700 dark:text-gray-300">{{ l.userEmail }}</td>
                    @if (multiPeriod()) {
                      <td class="whitespace-nowrap px-4 py-3 text-gray-700 dark:text-gray-300">{{ periodName(l.period, 'short') }}</td>
                    }
                    <td class="whitespace-nowrap px-4 py-3 text-right text-gray-700 dark:text-gray-300">{{ l.granted === null ? 'Uncapped' : usd(l.granted) }}</td>
                    <td class="whitespace-nowrap px-4 py-3 text-right font-medium text-gray-900 dark:text-white">{{ usd(l.consumed) }}</td>
                    <td class="whitespace-nowrap px-4 py-3 text-gray-700 dark:text-gray-300">{{ modeLabel(l) }}</td>
                    <td class="px-4 py-3 text-gray-700 dark:text-gray-300">{{ l.approvedBy }}</td>
                    <td class="whitespace-nowrap px-4 py-3 text-gray-700 dark:text-gray-300">{{ date(l.approvedAt) }}</td>
                  </tr>
                }
              }
            </tbody>
          }
        </table>
      </div>
    }

    @if (report(); as r) {
      <p class="mt-4 text-xs/5 text-gray-600 dark:text-gray-400">{{ r.consumptionOrder }}</p>
    }
  `,
})
export class ChargebackReportPage implements OnInit {
  private service = inject(QuotaRequestsAdminService);
  private dialog = inject(Dialog);

  readonly selectClass =
    'mt-1 rounded-2xl border border-gray-300 bg-white py-1.5 pl-3 pr-8 text-sm/6 text-gray-900 focus:border-primary-accessible focus:outline-none focus:ring-2 focus:ring-primary-accessible dark:border-gray-600 dark:bg-gray-900 dark:text-white';

  private readonly now = new Date();
  readonly currentMonth = monthOf(this.now);

  readonly rangeKind = signal<ChargebackRangeKind>('month');
  readonly anchor = signal(lastCompleteMonth(this.now));
  readonly fundFilter = signal('');
  readonly modeFilter = signal<ChargebackModeFilter | ''>('');
  readonly grouping = signal<Grouping>('fund');
  private readonly fiscalYearEnd = signal('12-31');

  readonly report = signal<ChargebackReport | null>(null);
  readonly funds = signal<ChargebackFund[]>([]);
  readonly loading = signal(false);
  readonly exporting = signal(false);
  readonly recalculating = signal<string | null>(null);
  readonly error = signal<string | null>(null);
  readonly notice = signal<string | null>(null);
  /** Groups whose open/closed state differs from the grouping's default. */
  private readonly toggled = signal<ReadonlySet<string>>(new Set());

  readonly range = computed(() => rangeFor(this.rangeKind(), this.anchor(), this.fiscalYearEnd(), this.now));
  readonly multiPeriod = computed(() => (this.report()?.periods.length ?? 0) > 1);
  private readonly fields = computed<FundingField[]>(() => this.report()?.fundingFields ?? []);

  readonly groupFieldLabel = computed(() => {
    const key = this.report()?.groupField;
    if (!key) return null;
    return this.fields().find(f => f.key === key)?.label ?? key;
  });

  readonly groups = computed<ChargeGroup[]>(() => {
    const report = this.report();
    if (!report) return [];
    const byGroup = this.grouping() === 'group' && this.groupFieldLabel() !== null;
    const map = new Map<string, ChargeGroup>();
    for (const line of report.lines) {
      const key = byGroup ? this.groupKeyOf(line) : (line.fundingKey ?? CENTRAL);
      let group = map.get(key);
      if (!group) {
        group = { key, ...this.groupHeading(line, byGroup), lines: [], consumed: 0, granted: 0 };
        map.set(key, group);
      }
      group.lines.push(line);
      group.consumed += toAmount(line.consumed);
      group.granted = group.granted === null || line.granted === null ? null : group.granted + toAmount(line.granted);
    }
    const groups = [...map.values()];
    // The central budget reads first, then funds by name.
    return groups.sort((a, b) => (a.key === CENTRAL ? -1 : b.key === CENTRAL ? 1 : a.title.localeCompare(b.title)));
  });

  ngOnInit(): void {
    this.service.getPolicy().then(
      policy => {
        this.fiscalYearEnd.set(policy.fiscalYearEnd);
        if (this.rangeKind() === 'fiscal_year') void this.load();
      },
      // Fiscal years fall back to the calendar year; the report itself still loads.
      () => undefined,
    );
    this.service.listFunds().then(
      funds => this.funds.set(funds),
      () => undefined,
    );
    void this.load();
  }

  setRangeKind(event: Event): void {
    this.rangeKind.set((event.target as HTMLSelectElement).value as ChargebackRangeKind);
    void this.load();
  }

  setAnchor(event: Event): void {
    const value = (event.target as HTMLInputElement).value;
    if (!/^\d{4}-\d{2}$/.test(value)) return;
    this.anchor.set(value);
    void this.load();
  }

  setFund(event: Event): void {
    this.fundFilter.set((event.target as HTMLSelectElement).value);
    void this.load();
  }

  setMode(event: Event): void {
    this.modeFilter.set((event.target as HTMLSelectElement).value as ChargebackModeFilter | '');
    void this.load();
  }

  setGrouping(event: Event): void {
    this.grouping.set((event.target as HTMLSelectElement).value as Grouping);
    this.toggled.set(new Set());
  }

  /** Funds start open; a grouping-field roll-up starts collapsed, one row per group, with a drill-down. */
  isOpen(key: string): boolean {
    return (this.grouping() === 'fund') !== this.toggled().has(key);
  }

  toggle(key: string): void {
    this.toggled.update(current => {
      const next = new Set(current);
      if (!next.delete(key)) next.add(key);
      return next;
    });
  }

  private query() {
    const { from, to } = this.range();
    return { from, to, fundingKey: this.fundFilter() || null, mode: this.modeFilter() || null };
  }

  async load(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      this.report.set(await this.service.getChargeback(this.query()));
    } catch (error) {
      this.report.set(null);
      this.error.set(quotaAdminErrorMessage(error, 'The chargeback report could not be loaded.'));
    } finally {
      this.loading.set(false);
    }
  }

  async exportCsv(): Promise<void> {
    this.exporting.set(true);
    this.error.set(null);
    try {
      const query = this.query();
      const blob = await this.service.getChargebackCsv(query);
      const span = query.from === query.to ? query.from : `${query.from}_to_${query.to}`;
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `chargeback_${span}.csv`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      this.error.set(quotaAdminErrorMessage(error, 'The CSV could not be exported.'));
    } finally {
      this.exporting.set(false);
    }
  }

  async recalculate(period: ChargebackPeriod): Promise<void> {
    this.notice.set(null);
    const ref = this.dialog.open<boolean, ConfirmationDialogData>(ConfirmationDialogComponent, {
      data: {
        title: `Recalculate ${this.periodName(period.period)}?`,
        message:
          'The month is rebuilt from today’s extensions and recorded spend, replacing its closed statement. ' +
          'Each fund’s total before and after goes to the audit log. Use this for a cost correction or a ' +
          'revoke that should have been back-dated.',
        confirmText: 'Recalculate',
        cancelText: 'Cancel',
      },
    });
    if (!(await firstValueFrom(ref.closed))) return;
    this.recalculating.set(period.period);
    this.error.set(null);
    try {
      const result = await this.service.reclosePeriod(period.period);
      this.notice.set(this.recloseSummary(period.period, result.changedFunds, result.before, result.after));
      await this.load();
    } catch (error) {
      this.error.set(quotaAdminErrorMessage(error, 'The month could not be recalculated.'));
    } finally {
      this.recalculating.set(null);
    }
  }

  private recloseSummary(
    period: string,
    changed: string[],
    before: Record<string, string>,
    after: Record<string, string>,
  ): string {
    const name = this.periodName(period);
    if (!changed.length) return `Recalculated ${name}. No funding source’s total changed.`;
    const parts = changed.map(key => {
      const label = this.fundNameByKey(key);
      return `${label} ${this.usd(before[key] ?? '0')} → ${this.usd(after[key] ?? '0')}`;
    });
    return `Recalculated ${name}. ${parts.join('; ')}.`;
  }

  private groupKeyOf(line: QuotaChargeLine): string {
    if (!line.fundingKey) return CENTRAL;
    return `group:${line.groupValue ?? ''}`;
  }

  private groupHeading(line: QuotaChargeLine, byGroup: boolean): { title: string; detail: string } {
    if (!line.fundingKey) return { title: 'Central budget', detail: 'Centrally funded extensions' };
    if (byGroup) {
      const label = this.groupFieldLabel() ?? '';
      return line.groupValue
        ? { title: `${label} ${line.groupValue}`, detail: 'All funding sources in this group' }
        : { title: `No ${label.toLowerCase()}`, detail: `Funding sources without a ${label.toLowerCase()}` };
    }
    const authorizer = line.authorizerEmail ? `Authorized by ${line.authorizerEmail}` : 'No authorizer named';
    return { title: this.fundName(line.fundingLabel, line.fields), detail: authorizer };
  }

  fundName(label: string, fields: Record<string, string>): string {
    return describeFundingSource({ label, fields }, this.fields()) || 'Funding source';
  }

  private fundNameByKey(key: string): string {
    if (key === CENTRAL) return 'Central budget';
    const fund = this.funds().find(f => f.fundingKey === key);
    const line = this.report()?.lines.find(l => l.fundingKey === key);
    if (fund) return this.fundName(fund.label, fund.fields);
    if (line) return this.fundName(line.fundingLabel, line.fields);
    return key;
  }

  closedText(period: ChargebackPeriod): string {
    const version = period.closeVersion && period.closeVersion > 1 ? ` · recalculated (v${period.closeVersion})` : '';
    return `on ${formatQuotaDate(period.closedAt)}${version}`;
  }

  provisionalText(period: ChargebackPeriod): string {
    if (period.closable) return 'closing now; reload in a moment';
    return `closes ${formatQuotaDate(closesAt(period.period).toISOString())}`;
  }

  modeLabel(line: QuotaChargeLine): string {
    if (line.standing) return 'Standing';
    return line.fundingMode === 'chargeback' ? 'Charge back' : 'Central';
  }

  periodName(period: string, style: 'long' | 'short' = 'long'): string {
    return formatPeriod(period, style);
  }

  shortId(id: string): string {
    return id.length > 8 ? `${id.slice(0, 8)}…` : id;
  }

  usd(value: DecimalString | null | undefined): string {
    return formatUsd(toAmount(value ?? '0'));
  }

  money(value: number): string {
    return formatUsd(value);
  }

  date(value: string | null | undefined): string {
    return formatQuotaDate(value);
  }
}
