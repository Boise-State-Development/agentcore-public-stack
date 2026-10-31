import { ChangeDetectionStrategy, Component, effect, inject, input, signal, untracked } from '@angular/core';
import { formatQuotaDate } from '../../../services/quota/quota-format';
import { FundingField, QuotaExtension, toAmount } from '../models/quota-requests-admin.models';
import { QuotaRequestsAdminService } from '../services/quota-requests-admin.service';
import { describeExtensionAmount, describeFundingSource } from '../utils/quota-admin-copy';

/**
 * A user's active extensions, on the quota card of their admin page (spec
 * §6.4), so support can answer "why wasn't this person blocked?" at a glance:
 * *Standing chargeback: Fund 100000, cap $200 per month, through Jun 30*.
 *
 * Only rendered for an admin holding `admin.quota_requests` in a build with
 * the feature on; the page decides that.
 */
@Component({
  selector: 'app-user-quota-extensions',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block' },
  template: `
    @if (extensions().length) {
      <ul class="mt-3 space-y-2" aria-label="Active quota extensions">
        @for (e of extensions(); track e.extensionId) {
          <li class="rounded-xs border border-state-info-200 bg-state-info-50 p-2 text-sm/6 text-state-info-900 dark:border-state-info-800 dark:bg-state-info-900/20 dark:text-state-info-200">
            {{ line(e) }}
          </li>
        }
      </ul>
    }
  `,
})
export class UserQuotaExtensionsComponent {
  private service = inject(QuotaRequestsAdminService);

  readonly userId = input.required<string>();

  readonly extensions = signal<QuotaExtension[]>([]);
  private fundingFields: FundingField[] = [];

  constructor() {
    effect(() => {
      const userId = this.userId();
      untracked(() => void this.reload(userId));
    });
  }

  async reload(userId = this.userId()): Promise<void> {
    try {
      const [extensions, policy] = await Promise.all([
        this.service.listExtensions({ userId, active: true }),
        this.fundingFields.length ? null : this.service.getPolicy().catch(() => null),
      ]);
      if (policy) this.fundingFields = policy.fundingFields;
      this.extensions.set(extensions);
    } catch {
      // The card still shows the tier; an unreadable extension list is not worth an error box here.
      this.extensions.set([]);
    }
  }

  line(extension: QuotaExtension): string {
    const amount = extension.amount === null ? null : toAmount(extension.amount);
    const through = `through ${formatQuotaDate(extension.validUntil)}`;
    if (extension.standing) {
      const fund = describeFundingSource(extension.fundingSource, this.fundingFields) || 'a funding source';
      const cap = amount === null ? 'no cap' : `cap ${describeExtensionAmount(amount, true).replace(/^Cap /, '')}`;
      return `Standing chargeback: ${fund}, ${cap}, ${through}`;
    }
    const mode = extension.fundingMode === 'chargeback' ? 'charged back' : 'centrally funded';
    return `Extension: ${describeExtensionAmount(amount, false)}, ${mode}, ${through}`;
  }
}
