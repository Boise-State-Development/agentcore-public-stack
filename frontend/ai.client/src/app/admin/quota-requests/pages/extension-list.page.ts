import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { formatQuotaDate } from '../../../services/quota/quota-format';
import {
  ExtendQuotaDialogComponent,
  ExtendQuotaDialogData,
  ExtendQuotaDialogResult,
} from '../components/extend-quota-dialog.component';
import {
  QuotaNoteDialogComponent,
  QuotaNoteDialogData,
  QuotaNoteDialogResult,
} from '../components/quota-note-dialog.component';
import { AdminQuotaRequestPolicy, QuotaExtension, toAmount } from '../models/quota-requests-admin.models';
import { QuotaRequestsAdminService, quotaAdminErrorMessage } from '../services/quota-requests-admin.service';
import { describeExtensionAmount, describeFundingSource } from '../utils/quota-admin-copy';

type KindFilter = 'all' | 'standing' | 'one_time';
type ExtensionState = 'active' | 'scheduled' | 'ended' | 'revoked';

/**
 * Active and recent extensions (spec §6.5). The *Standing* filter lists every
 * chargeback arrangement a deployment carries: the list a finance office asks
 * for at year end. Revoke ends one now with a reason the user is sent; nothing
 * is ever deleted.
 *
 * "Consumed so far this period" arrives with the chargeback report (PR-4),
 * which owns the waterfall that attributes spend to an extension.
 */
@Component({
  selector: 'app-extension-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  host: { class: 'block' },
  template: `
    <div class="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div class="flex flex-wrap items-center gap-4">
        <div>
          <label for="extension-kind" class="block text-xs/5 font-medium text-gray-700 dark:text-gray-300">Kind</label>
          <select id="extension-kind" [value]="kindFilter()" (change)="setKind($event)"
            class="mt-1 rounded-2xl border border-gray-300 bg-white py-1.5 pl-3 pr-8 text-sm/6 text-gray-900 focus:border-primary-accessible focus:outline-none focus:ring-2 focus:ring-primary-accessible dark:border-gray-600 dark:bg-gray-900 dark:text-white">
            <option value="all">All</option>
            <option value="standing">Standing</option>
            <option value="one_time">One-time</option>
          </select>
        </div>
        <label class="mt-5 flex items-center gap-2 text-sm/6 text-gray-900 dark:text-white">
          <input type="checkbox" [checked]="activeOnly()" (change)="setActive($event)"
            class="size-4 rounded-sm border-gray-300 text-primary-accessible focus:ring-primary-accessible" />
          Active only
        </label>
      </div>
      <button type="button" (click)="newExtension()"
        class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500">
        New extension
      </button>
    </div>

    @if (error(); as message) {
      <p role="alert" class="mb-4 rounded-2xl bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">{{ message }}</p>
    }
    @if (notice(); as message) {
      <p role="status" class="mb-4 rounded-2xl bg-state-success-50 px-4 py-3 text-sm/6 text-state-success-900 dark:bg-state-success-900/20 dark:text-state-success-200">{{ message }}</p>
    }

    @if (loading() && !extensions().length) {
      <p class="py-10 text-center text-sm/6 text-gray-600 dark:text-gray-400">Loading extensions…</p>
    } @else if (!visible().length) {
      <p class="rounded-2xl border border-dashed border-gray-300 py-10 text-center text-sm/6 text-gray-600 dark:border-gray-600 dark:text-gray-400">No extensions match.</p>
    } @else {
      <div class="relative overflow-x-auto rounded-2xl border border-gray-200 dark:border-white/10">
        <table class="min-w-full divide-y divide-gray-200 text-left text-sm/6 dark:divide-white/10">
          <thead class="bg-gray-50 dark:bg-white/5">
            <tr>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">User</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Kind</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Amount</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Period</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Funding</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">From</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Created by</th>
              <th scope="col" class="px-4 py-2 font-semibold text-gray-900 dark:text-white">Status</th>
              <th scope="col" class="px-4 py-2"><span class="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-200 bg-white dark:divide-white/10 dark:bg-gray-900">
            @for (e of visible(); track e.extensionId) {
              <tr>
                <td class="px-4 py-3 text-gray-900 dark:text-white">{{ e.userEmail }}</td>
                <td class="whitespace-nowrap px-4 py-3 text-gray-700 dark:text-gray-300">{{ e.standing ? 'Standing' : 'One-time' }}</td>
                <td class="whitespace-nowrap px-4 py-3 text-gray-900 dark:text-white">{{ amount(e) }}</td>
                <td class="whitespace-nowrap px-4 py-3 text-gray-700 dark:text-gray-300">{{ date(e.validFrom) }} – {{ date(e.validUntil) }}</td>
                <td class="max-w-[14rem] px-4 py-3 text-gray-700 dark:text-gray-300">
                  {{ e.fundingMode === 'chargeback' ? 'Charge back' : 'Central' }}
                  @if (fundingLabel(e); as label) {
                    <span class="block truncate text-xs/5 text-gray-600 dark:text-gray-400" [title]="label">{{ label }}</span>
                  }
                </td>
                <td class="whitespace-nowrap px-4 py-3">
                  @if (e.requestId) {
                    <a [routerLink]="['/admin/quota-requests/requests', e.requestId]" class="text-primary-accessible hover:underline dark:text-primary-accessible-dark">Request</a>
                  } @else {
                    <span class="text-gray-700 dark:text-gray-300" [title]="e.reason">Direct</span>
                  }
                </td>
                <td class="px-4 py-3 text-gray-700 dark:text-gray-300">{{ e.createdByEmail || 'An admin' }}</td>
                <td class="whitespace-nowrap px-4 py-3">
                  <span class="rounded-full px-2 py-0.5 text-xs/5 font-medium" [class]="stateClass(stateOf(e))">{{ stateLabel(stateOf(e)) }}</span>
                  @if (e.revokeReason) {
                    <span class="mt-1 block max-w-[12rem] truncate text-xs/5 text-gray-600 dark:text-gray-400" [title]="e.revokeReason">{{ e.revokeReason }}</span>
                  }
                </td>
                <td class="whitespace-nowrap px-4 py-3 text-right">
                  @if (stateOf(e) === 'active' || stateOf(e) === 'scheduled') {
                    <button type="button" (click)="revoke(e)" [attr.aria-label]="'Revoke the extension for ' + e.userEmail"
                      class="rounded-2xl px-3 py-1 text-sm/6 font-medium text-state-danger-700 hover:bg-state-danger-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-500 dark:text-state-danger-300 dark:hover:bg-state-danger-900/20">
                      Revoke
                    </button>
                  }
                </td>
              </tr>
            }
          </tbody>
        </table>
      </div>
    }
  `,
})
export class ExtensionListPage implements OnInit {
  private service = inject(QuotaRequestsAdminService);
  private dialog = inject(Dialog);

  readonly extensions = signal<QuotaExtension[]>([]);
  readonly kindFilter = signal<KindFilter>('all');
  readonly activeOnly = signal(true);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly notice = signal<string | null>(null);

  private policy: AdminQuotaRequestPolicy | null = null;
  /** The schema's labels, so a fund reads "Fund 100000 · Org 4210" rather than its keys. */
  private readonly fundingFields = signal<AdminQuotaRequestPolicy['fundingFields']>([]);

  readonly visible = computed(() => {
    const kind = this.kindFilter();
    return this.extensions().filter(e => kind === 'all' || (kind === 'standing') === e.standing);
  });

  ngOnInit(): void {
    void this.load();
    this.service.getPolicy().then(
      policy => {
        this.policy = policy;
        this.fundingFields.set(policy.fundingFields);
      },
      // Labels fall back to the field keys; New extension retries the read.
      () => undefined,
    );
  }

  setKind(event: Event): void {
    this.kindFilter.set((event.target as HTMLSelectElement).value as KindFilter);
  }

  setActive(event: Event): void {
    this.activeOnly.set((event.target as HTMLInputElement).checked);
    void this.load();
  }

  async load(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      this.extensions.set(await this.service.listExtensions({ active: this.activeOnly() }));
    } catch (error) {
      this.error.set(quotaAdminErrorMessage(error, 'Extensions could not be loaded.'));
    } finally {
      this.loading.set(false);
    }
  }

  async newExtension(): Promise<void> {
    this.notice.set(null);
    try {
      this.policy ??= await this.service.getPolicy();
    } catch (error) {
      this.error.set(quotaAdminErrorMessage(error, 'The quota request settings could not be loaded.'));
      return;
    }
    const ref = this.dialog.open<ExtendQuotaDialogResult, ExtendQuotaDialogData>(ExtendQuotaDialogComponent, {
      data: { mode: 'direct', policy: this.policy },
    });
    const result = await firstValueFrom(ref.closed);
    if (result?.kind !== 'created') return;
    this.notice.set(`Extension created for ${result.extension.userEmail}. They have been notified.`);
    await this.load();
  }

  async revoke(extension: QuotaExtension): Promise<void> {
    this.notice.set(null);
    const ref = this.dialog.open<QuotaNoteDialogResult<QuotaExtension>, QuotaNoteDialogData<QuotaExtension>>(
      QuotaNoteDialogComponent,
      {
        data: {
          title: 'Revoke this extension',
          description: `It ends now for ${extension.userEmail}, who is sent your reason. The record stays for reporting.`,
          label: 'Reason',
          confirmLabel: 'Revoke',
          tone: 'danger',
          submit: reason => this.service.revokeExtension(extension.extensionId, reason),
        },
      },
    );
    const result = await firstValueFrom(ref.closed);
    if (!result) return;
    this.notice.set(`Revoked. ${extension.userEmail} has been notified.`);
    await this.load();
  }

  stateOf(extension: QuotaExtension, now: Date = new Date()): ExtensionState {
    if (extension.revokedAt) return 'revoked';
    const nowIso = now.toISOString();
    if (!extension.enabled || extension.validUntil < nowIso) return 'ended';
    if (extension.validFrom > nowIso) return 'scheduled';
    return 'active';
  }

  stateLabel(state: ExtensionState): string {
    return { active: 'Active', scheduled: 'Starts later', ended: 'Ended', revoked: 'Revoked' }[state];
  }

  stateClass(state: ExtensionState): string {
    return state === 'active' || state === 'scheduled'
      ? 'bg-state-success-100 text-state-success-900 dark:bg-state-success-900/30 dark:text-state-success-200'
      : 'bg-gray-100 text-gray-800 dark:bg-white/10 dark:text-gray-200';
  }

  amount(extension: QuotaExtension): string {
    return describeExtensionAmount(extension.amount === null ? null : toAmount(extension.amount), extension.standing);
  }

  fundingLabel(extension: QuotaExtension): string {
    return describeFundingSource(extension.fundingSource, this.fundingFields());
  }

  date(value: string): string {
    return formatQuotaDate(value);
  }
}
