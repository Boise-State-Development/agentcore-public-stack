import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { UserService } from '../../../auth/user.service';
import { formatAmount, formatQuotaDate, perPeriod } from '../../../services/quota/quota-format';
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
import {
  AdminQuotaRequest,
  AdminQuotaRequestPolicy,
  QuotaExtension,
  QuotaRequestContext,
  QuotaRequestDetail,
  QuotaRequestStatus,
  toAmount,
} from '../models/quota-requests-admin.models';
import { QuotaRequestsAdminService, quotaAdminErrorMessage } from '../services/quota-requests-admin.service';
import { STATUS_LABELS, STATUS_PILL_CLASSES, describeExtensionAmount, describeFundingSource } from '../utils/quota-admin-copy';

/**
 * One request with everything needed to decide it (spec §6.3): who asked and
 * how their tier resolved, usage now against usage when they asked, the last
 * three periods, the full justification and funding source, and their history.
 *
 * Approve opens the grant form; Ask for info and Deny each take a required
 * note that goes to the user verbatim. The buttons follow what the API allows:
 * info only from pending, approve and deny from pending or waiting on the
 * user, and none at all on your own request (the API refuses with 403).
 */
@Component({
  selector: 'app-request-detail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  host: { class: 'block' },
  template: `
    <a routerLink="/admin/quota-requests/requests" class="text-sm/6 font-medium text-primary-accessible hover:underline dark:text-primary-accessible-dark">
      ← All requests
    </a>

    @if (error(); as message) {
      <p role="alert" class="mt-4 rounded-2xl bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">{{ message }}</p>
    }
    @if (notice(); as message) {
      <p role="status" class="mt-4 rounded-2xl bg-state-success-50 px-4 py-3 text-sm/6 text-state-success-900 dark:bg-state-success-900/20 dark:text-state-success-200">{{ message }}</p>
    }

    @if (detail(); as d) {
      <div class="mt-4 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 class="text-xl/8 font-semibold text-gray-900 dark:text-white">{{ d.user.name || d.request.userEmail }}</h2>
          <p class="text-sm/6 text-gray-600 dark:text-gray-400">
            {{ d.request.userEmail }} · asked {{ date(d.request.createdAt) }}
          </p>
        </div>
        <span class="rounded-full px-3 py-1 text-sm/6 font-medium" [class]="statusClass(d.request.status)">{{ statusLabel(d.request.status) }}</span>
      </div>

      @if (isOwn()) {
        <p class="mt-4 rounded-2xl bg-state-info-50 px-4 py-3 text-sm/6 text-state-info-900 dark:bg-state-info-900/20 dark:text-state-info-200">
          This is your own request. Another admin has to decide it.
        </p>
      } @else if (open()) {
        <div class="mt-4 flex flex-wrap gap-2">
          <button type="button" (click)="approve()"
            class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500">
            Approve…
          </button>
          @if (d.request.status === 'pending') {
            <button type="button" (click)="askForInfo()"
              class="rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700">
              Ask for info…
            </button>
          }
          <button type="button" (click)="deny()"
            class="rounded-2xl border border-state-danger-300 bg-white px-4 py-2 text-sm/6 font-medium text-state-danger-700 hover:bg-state-danger-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-500 dark:border-state-danger-700 dark:bg-gray-800 dark:text-state-danger-300 dark:hover:bg-state-danger-900/20">
            Deny…
          </button>
        </div>
      }

      <div class="mt-6 grid gap-6 lg:grid-cols-3">
        <section class="rounded-2xl border border-gray-200 p-5 dark:border-white/10 lg:col-span-2" aria-labelledby="request-heading">
          <h3 id="request-heading" class="text-base/7 font-semibold text-gray-900 dark:text-white">The request</h3>
          <dl class="mt-3 grid gap-x-6 gap-y-3 text-sm/6 sm:grid-cols-2">
            <div>
              <dt class="text-gray-600 dark:text-gray-400">Asked for</dt>
              <dd class="font-medium text-gray-900 dark:text-white">{{ askedFor(d.request) }}</dd>
            </div>
            <div>
              <dt class="text-gray-600 dark:text-gray-400">Through</dt>
              <dd class="font-medium text-gray-900 dark:text-white">{{ date(d.request.requestedUntil) }}</dd>
            </div>
            <div class="sm:col-span-2">
              <dt class="text-gray-600 dark:text-gray-400">Why</dt>
              <dd class="whitespace-pre-line text-gray-900 dark:text-white">{{ d.request.justification }}</dd>
            </div>
            <div class="sm:col-span-2">
              <dt class="text-gray-600 dark:text-gray-400">Funding source</dt>
              <dd class="text-gray-900 dark:text-white">
                {{ fundingText(d.request) || 'None given' }}
                @if (d.request.fundingSource?.authorizerEmail) {
                  <span class="text-gray-600 dark:text-gray-400"> · authorizer {{ d.request.fundingSource?.authorizerEmail }}</span>
                }
              </dd>
            </div>
            @if (d.request.adminNote) {
              <div class="sm:col-span-2">
                <dt class="text-gray-600 dark:text-gray-400">Asked of the user</dt>
                <dd class="whitespace-pre-line text-gray-900 dark:text-white">{{ d.request.adminNote }}</dd>
              </div>
            }
            @if (d.request.decision; as decision) {
              <div class="sm:col-span-2">
                <dt class="text-gray-600 dark:text-gray-400">Decision</dt>
                <dd class="text-gray-900 dark:text-white">
                  {{ decision.outcome === 'approved' ? 'Approved' : 'Denied' }} {{ date(decision.decidedAt) }}
                  @if (decision.decidedByEmail) { by {{ decision.decidedByEmail }} }
                  @if (decision.outcome === 'approved') {
                    · {{ grantedText(decision.amount ?? null, !!decision.standing, d.request.context.periodType) }}
                    through {{ date(decision.validUntil ?? '') }} · {{ decision.fundingMode === 'chargeback' ? 'charged back' : 'centrally funded' }}
                  }
                  @if (decision.note) {
                    <span class="mt-1 block whitespace-pre-line text-gray-700 dark:text-gray-300">“{{ decision.note }}”</span>
                  }
                </dd>
              </div>
            }
          </dl>
        </section>

        <section class="rounded-2xl border border-gray-200 p-5 dark:border-white/10" aria-labelledby="usage-heading">
          <h3 id="usage-heading" class="text-base/7 font-semibold text-gray-900 dark:text-white">Usage</h3>
          <dl class="mt-3 space-y-3 text-sm/6">
            <div>
              <dt class="text-gray-600 dark:text-gray-400">Now ({{ d.live.period }})</dt>
              <dd class="font-medium text-gray-900 dark:text-white">
                {{ usage(d.live) }}
                @if (d.live.blocked) { <span class="font-semibold text-state-danger-700 dark:text-state-danger-300">· blocked</span> }
              </dd>
            </div>
            <div>
              <dt class="text-gray-600 dark:text-gray-400">When they asked</dt>
              <dd class="text-gray-900 dark:text-white">{{ usage(d.request.context) }} @if (d.request.context.blocked) { · blocked }</dd>
            </div>
            <div>
              <dt class="text-gray-600 dark:text-gray-400">Tier</dt>
              <dd class="text-gray-900 dark:text-white">
                {{ d.live.tierName || 'None' }}
                @if (d.user.matchedBy) { <span class="text-gray-600 dark:text-gray-400">({{ d.user.matchedBy }})</span> }
              </dd>
            </div>
            <div>
              <dt class="text-gray-600 dark:text-gray-400">Previous periods</dt>
              <dd>
                <ul class="text-gray-900 dark:text-white">
                  @for (p of d.previousPeriods; track p.period) {
                    <li>{{ p.period }}: \${{ p.spend.toFixed(2) }}</li>
                  }
                </ul>
              </dd>
            </div>
            @if (d.user.roles.length) {
              <div>
                <dt class="text-gray-600 dark:text-gray-400">Roles</dt>
                <dd class="text-gray-900 dark:text-white">{{ d.user.roles.join(', ') }}</dd>
              </div>
            }
          </dl>
          @if (canOpenUser()) {
            <a [routerLink]="['/admin/users', d.user.userId]" class="mt-4 inline-block text-sm/6 font-medium text-primary-accessible hover:underline dark:text-primary-accessible-dark">
              Open their user page and conversation costs →
            </a>
          }
        </section>
      </div>

      <section class="mt-6 rounded-2xl border border-gray-200 p-5 dark:border-white/10" aria-labelledby="history-heading">
        <h3 id="history-heading" class="text-base/7 font-semibold text-gray-900 dark:text-white">History</h3>
        @if (!d.priorRequests.length && !d.extensions.length) {
          <p class="mt-2 text-sm/6 text-gray-600 dark:text-gray-400">No earlier requests or extensions.</p>
        }
        @if (d.extensions.length) {
          <h4 class="mt-3 text-sm/6 font-semibold text-gray-900 dark:text-white">Extensions</h4>
          <ul class="mt-1 space-y-1 text-sm/6 text-gray-700 dark:text-gray-300">
            @for (e of d.extensions; track e.extensionId) {
              <li>{{ extensionLine(e) }}</li>
            }
          </ul>
        }
        @if (d.priorRequests.length) {
          <h4 class="mt-3 text-sm/6 font-semibold text-gray-900 dark:text-white">Earlier requests</h4>
          <ul class="mt-1 space-y-1 text-sm/6 text-gray-700 dark:text-gray-300">
            @for (r of d.priorRequests; track r.requestId) {
              <li>
                <a [routerLink]="['/admin/quota-requests/requests', r.requestId]" class="text-primary-accessible hover:underline dark:text-primary-accessible-dark">{{ date(r.createdAt) }}</a>
                · {{ askedFor(r) }} · {{ statusLabel(r.status) }}
              </li>
            }
          </ul>
        }
      </section>
    } @else if (loading()) {
      <p class="py-10 text-center text-sm/6 text-gray-600 dark:text-gray-400">Loading the request…</p>
    }
  `,
})
export class RequestDetailPage implements OnInit {
  private route = inject(ActivatedRoute);
  private dialog = inject(Dialog);
  private service = inject(QuotaRequestsAdminService);
  private userService = inject(UserService);

  readonly detail = signal<QuotaRequestDetail | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly notice = signal<string | null>(null);

  private policy: AdminQuotaRequestPolicy | null = null;
  /** The schema's labels, so a fund reads "Fund 100000 · Org 4210" rather than its keys. */
  private readonly fundingFields = signal<AdminQuotaRequestPolicy['fundingFields']>([]);

  readonly open = computed(() => {
    const status = this.detail()?.request.status;
    return status === 'pending' || status === 'info_requested';
  });

  readonly isOwn = computed(() => {
    const me = this.userService.currentUser()?.user_id;
    return !!me && this.detail()?.request.userId === me;
  });

  readonly canOpenUser = computed(() => this.userService.hasAdminScope('admin.users'));

  private get requestId(): string {
    return this.route.snapshot.paramMap.get('requestId') ?? '';
  }

  ngOnInit(): void {
    void this.load();
    // Labels fall back to the field keys; Approve retries the read.
    this.loadPolicy().catch(() => undefined);
  }

  async load(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      this.detail.set(await this.service.getRequest(this.requestId));
    } catch (error) {
      this.error.set(quotaAdminErrorMessage(error, 'This request could not be loaded.'));
    } finally {
      this.loading.set(false);
    }
  }

  private async loadPolicy(): Promise<AdminQuotaRequestPolicy> {
    this.policy ??= await this.service.getPolicy();
    this.fundingFields.set(this.policy.fundingFields);
    return this.policy;
  }

  async approve(): Promise<void> {
    const detail = this.detail();
    if (!detail) return;
    this.notice.set(null);
    let policy: AdminQuotaRequestPolicy;
    try {
      policy = await this.loadPolicy();
    } catch (error) {
      this.error.set(quotaAdminErrorMessage(error, 'The quota request settings could not be loaded.'));
      return;
    }
    const ref = this.dialog.open<ExtendQuotaDialogResult, ExtendQuotaDialogData>(ExtendQuotaDialogComponent, {
      data: {
        mode: 'approve',
        request: detail.request,
        policy,
        userLabel: detail.user.name || detail.request.userEmail,
      },
    });
    const result = await firstValueFrom(ref.closed);
    if (!result) return;
    if (result.kind === 'askForFunding') {
      await this.askForInfo('Which fund or account should this be charged to? Add it to your request and it comes back to us.');
      return;
    }
    if (result.kind === 'approved') {
      this.notice.set('Approved. They have been notified, and the extension is in the Extensions tab.');
      await this.load();
    }
  }

  async askForInfo(prefill = ''): Promise<void> {
    await this.decide({
      title: 'Ask for more information',
      description: 'The request goes back to the user with your note, verbatim. Used mostly for "which fund?".',
      label: 'What do you need from them?',
      initialNote: prefill || undefined,
      confirmLabel: 'Send to user',
      tone: 'primary',
      submit: note => this.service.requestInfo(this.requestId, note),
    }, 'Sent back to the user. It returns to Pending when they answer.');
  }

  async deny(): Promise<void> {
    await this.decide({
      title: 'Deny this request',
      description: 'Your note goes to the user verbatim, so write it for them.',
      label: 'Why not?',
      confirmLabel: 'Deny',
      tone: 'danger',
      submit: note => this.service.deny(this.requestId, note),
    }, 'Denied. The user has been notified with your note.');
  }

  private async decide(data: QuotaNoteDialogData<AdminQuotaRequest>, done: string): Promise<void> {
    this.notice.set(null);
    const ref = this.dialog.open<QuotaNoteDialogResult<AdminQuotaRequest>, QuotaNoteDialogData<AdminQuotaRequest>>(
      QuotaNoteDialogComponent,
      { data },
    );
    const result = await firstValueFrom(ref.closed);
    if (!result) return;
    this.notice.set(done);
    await this.load();
  }

  date(value: string | null | undefined): string {
    return formatQuotaDate(value);
  }

  askedFor(request: AdminQuotaRequest): string {
    const amount = formatAmount(toAmount(request.requestedAmount));
    const per = perPeriod(request.context.periodType);
    return request.kind === 'standing' ? `Standing chargeback, cap ${amount} ${per}` : `+${amount} ${per}`;
  }

  grantedText(amount: string | number | null, standing: boolean, periodType: string): string {
    return describeExtensionAmount(amount === null ? null : toAmount(amount), standing, perPeriod(periodType));
  }

  usage(context: QuotaRequestContext): string {
    const used = toAmount(context.currentUsage);
    const limit = toAmount(context.effectiveLimit ?? context.baseLimit);
    const usedText = Number.isFinite(used) ? `$${used.toFixed(2)}` : '—';
    if (!Number.isFinite(limit)) return `${usedText} of an unlimited tier`;
    const percent = toAmount(context.percentageUsed);
    return `${usedText} of ${formatAmount(limit)}${Number.isFinite(percent) ? ` (${Math.round(percent)}%)` : ''}`;
  }

  fundingText(request: AdminQuotaRequest): string {
    return describeFundingSource(request.fundingSource, this.fundingFields());
  }

  extensionLine(extension: QuotaExtension): string {
    const amount = describeExtensionAmount(
      extension.amount === null ? null : toAmount(extension.amount),
      extension.standing,
    );
    const mode = extension.fundingMode === 'chargeback' ? 'charged back' : 'central';
    const state = extension.revokedAt ? `revoked ${this.date(extension.revokedAt)}` : `through ${this.date(extension.validUntil)}`;
    return `${extension.standing ? 'Standing · ' : ''}${amount} · ${mode} · ${state}`;
  }

  statusLabel(status: QuotaRequestStatus): string {
    return STATUS_LABELS[status];
  }

  statusClass(status: QuotaRequestStatus): string {
    return STATUS_PILL_CLASSES[status];
  }
}
