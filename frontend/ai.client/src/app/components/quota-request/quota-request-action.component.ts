import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';
import { FEATURES } from '../../services/features';
import { QuotaStatusService } from '../../services/quota/quota-status.service';
import { QuotaRequestFailure, QuotaRequestService } from '../../services/quota/quota-request.service';
import { formatQuotaDate } from '../../services/quota/quota-format';
import { QuotaRequestDialogService } from './quota-request-dialog.service';

/**
 * What a quota entry point offers (quota-requests spec §5.1): *Request more*,
 * or, while a request is open, its state with the next step —
 * `Request pending · sent Oct 30 · Withdraw`, or
 * `Your admin asked for a funding source · Add it`.
 *
 * Renders nothing while `features.quotaRequests` is off, before the quota
 * status has loaded, and for a user with no quota or an unlimited one.
 *
 * `inline` sits inside the composer's quota tab (small text, link-styled);
 * `button` sits beside the Usage page's quota bar.
 */
@Component({
  selector: 'app-quota-request-action',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'contents' },
  template: `
    @if (state(); as s) {
      @switch (s.kind) {
        @case ('info') {
          <span [class]="textClass()">
            Your admin asked for a funding source ·
            <button type="button" [class]="linkClass()" (click)="addFunding(s.requestId, s.adminNote)">Add it</button>
          </span>
        }
        @case ('pending') {
          <span [class]="textClass()">
            Request pending · sent {{ s.sent }} ·
            <button type="button" [class]="linkClass()" [disabled]="busy()" (click)="withdraw(s.requestId)">Withdraw</button>
          </span>
        }
        @default {
          @if (variant() === 'button') {
            <button
              type="button"
              (click)="request()"
              class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-800 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100 dark:hover:bg-gray-700"
            >
              Request more quota
            </button>
          } @else {
            <button type="button" [class]="linkClass()" (click)="request()">{{ label() }}</button>
          }
        }
      }
      @if (error(); as message) {
        <span role="alert" class="text-state-danger-700 dark:text-state-danger-300" [class]="variant() === 'inline' ? 'text-xs' : 'text-sm/6'">{{ message }}</span>
      }
    }
  `,
})
export class QuotaRequestActionComponent {
  readonly variant = input<'inline' | 'button'>('inline');
  /** The inline link's wording: `Request more` in the warning tab, `Request more quota` when blocked. */
  readonly label = input('Request more');

  private features = inject(FEATURES);
  private quotaStatus = inject(QuotaStatusService);
  private requests = inject(QuotaRequestService);
  private dialog = inject(QuotaRequestDialogService);

  protected readonly busy = signal(false);
  protected readonly error = signal<string | null>(null);

  protected readonly state = computed(() => {
    if (!this.features.quotaRequests) return null;
    const status = this.quotaStatus.status.value();
    if (!status?.configured || status.unlimited) return null;
    const open = status.openRequest;
    if (open?.status === 'info_requested') {
      return { kind: 'info' as const, requestId: open.requestId, adminNote: open.adminNote ?? null };
    }
    if (open) {
      return { kind: 'pending' as const, requestId: open.requestId, sent: formatQuotaDate(open.createdAt, false) };
    }
    return { kind: 'request' as const };
  });

  protected readonly textClass = computed(() =>
    this.variant() === 'inline' ? 'text-xs' : 'text-sm/6 text-gray-700 dark:text-gray-300',
  );

  protected readonly linkClass = computed(
    () =>
      'rounded font-medium underline underline-offset-2 hover:no-underline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-primary-500 disabled:opacity-60 ' +
      (this.variant() === 'inline' ? 'text-xs' : 'text-sm/6 text-primary-accessible dark:text-primary-300'),
  );

  protected request(): void {
    this.error.set(null);
    void this.dialog.open();
  }

  protected addFunding(requestId: string, adminNote: string | null): void {
    this.error.set(null);
    void this.dialog.open({ fundingFor: { requestId, adminNote } });
  }

  protected async withdraw(requestId: string): Promise<void> {
    this.busy.set(true);
    this.error.set(null);
    try {
      await this.requests.withdraw(requestId);
    } catch (e) {
      this.error.set(e instanceof QuotaRequestFailure ? e.message : 'Couldn’t withdraw the request.');
    } finally {
      this.busy.set(false);
    }
  }
}
