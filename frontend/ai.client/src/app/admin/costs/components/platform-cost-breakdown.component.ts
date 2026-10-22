import {
  Component,
  ChangeDetectionStrategy,
  computed,
  input,
} from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroCheckCircle,
  heroExclamationTriangle,
  heroInformationCircle,
} from '@ng-icons/heroicons/outline';
import { PlatformCostSummary, PlatformServiceCost } from '../models';
import { getCategoricalColor } from '../../../shared/constants/chart-colors.constants';

/**
 * Platform cost breakdown — the AWS bill beside our own inference ledger.
 *
 * The dashboard measured token spend well and everything else not at all. On
 * prod that was 38.8% of the bill unaccounted for, with `Amazon Bedrock
 * AgentCore` at $201.54/month the second-largest line in the account and on
 * no screen anywhere.
 *
 * Three panels, in the order an admin needs them:
 *
 *  1. Where the money goes — inference vs platform, and what each costs per
 *     user per month. The all-in figure leads because it is the true unit
 *     economic; inference alone understated prod by 1.64x.
 *  2. Reconciliation — our ledger against Cost Explorer's own model SKUs.
 *     This is a *pricing regression test*: the two agreed to 0.50% when this
 *     shipped, and a widening gap means `curated-models.ts` has drifted from
 *     what AWS actually charges. CLAUDE.md names that as a live risk.
 *  3. The per-service table, with what was excluded shown rather than hidden.
 *
 * Deliberately a table and not a chart: these rows are read for exact
 * dollars and compared against an AWS console figure, which is a job a
 * sorted table with a share bar does better than a pie.
 */
@Component({
  selector: 'app-platform-cost-breakdown',
  imports: [DecimalPipe, NgIcon],
  providers: [
    provideIcons({ heroCheckCircle, heroExclamationTriangle, heroInformationCircle }),
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (!summary()) {
      <div
        class="rounded-2xl border border-gray-200 bg-white p-6 shadow-xs dark:border-gray-700 dark:bg-gray-800"
      >
        <p class="text-sm/6 text-gray-500 dark:text-gray-400">
          Loading platform costs…
        </p>
      </div>
    } @else if (!summary()!.available) {
      <!-- Not an error state. The sync is opt-in per environment, so "off"
           is a normal answer — and a $0.00 platform cost would read as "the
           infrastructure is free" rather than "nobody has measured it". -->
      <div
        class="rounded-2xl border border-gray-200 bg-white p-6 shadow-xs dark:border-gray-700 dark:bg-gray-800"
      >
        <div class="flex items-start gap-3">
          <!-- Colour and size live on the WRAPPER, never on <ng-icon>.
               ng-icon's own :host rule sets width/height from
               --ng-icon__size unlayered, which outranks Tailwind's
               @layer utilities size-*, and its colour rule sits in
               @layer ng-icon (registered after Tailwind's layers) resolving
               to currentColor. Measured: size-5 rendered 16px and
               text-state-warning-600 resolved to black, while the same
               class on a plain span resolved correctly. So: span carries
               the colour, [size] carries the dimension, glyph inherits. -->
          <span class="mt-0.5 shrink-0 text-gray-400 dark:text-gray-500">
            <ng-icon name="heroInformationCircle" size="1.25rem" aria-hidden="true" />
          </span>
          <div>
            <h3 class="text-base/7 font-semibold text-gray-900 dark:text-white">
              Platform costs not synced for this period
            </h3>
            <p class="mt-1 max-w-2xl text-sm/6 text-gray-500 dark:text-gray-400">
              The dashboard is showing inference cost only
              ({{ formatCurrency(summary()!.inferenceCost) }}). AWS
              infrastructure — ECS, AgentCore session hours, NAT egress,
              CloudWatch — is not included, and on a comparable environment
              that has been roughly 39% of the true bill.
            </p>
            <p class="mt-3 max-w-2xl text-xs/5 text-gray-500 dark:text-gray-400">
              Enable it by deploying the platform stack with
              <code class="rounded bg-gray-100 px-1 py-0.5 font-mono dark:bg-gray-700">CDK_PLATFORM_COSTS_ENABLED=true</code>.
              The daily sync reads AWS Cost Explorer, which requires
              <code class="rounded bg-gray-100 px-1 py-0.5 font-mono dark:bg-gray-700">ce:GetCostAndUsage</code>
              and bills $0.01 per request (about $0.30/year at one sync a day).
            </p>
          </div>
        </div>
      </div>
    } @else {
      <div class="space-y-6">
        <!-- Unscoped call to action. FIRST on the tab, above the numbers it
             qualifies: a caveat printed after the figures is one people read
             second, if at all. The heading states the FIX, not the symptom —
             "these figures cover the whole account" describes a condition and
             leaves the reader to hunt for what to do about it.

             Only rendered while it applies. Once the tag is activated this
             disappears on its own, so it never becomes furniture people learn
             to scroll past. -->
        @if (!scopedToDeployment()) {
          <div
            class="rounded-2xl border border-state-warning-300 bg-state-warning-50 p-6 dark:border-state-warning-800 dark:bg-state-warning-900/20"
          >
            <div class="flex items-start gap-3">
              <span class="mt-0.5 shrink-0 text-state-warning-700 dark:text-state-warning-400">
                <ng-icon name="heroExclamationTriangle" size="1.5rem" aria-hidden="true" />
              </span>
              <div class="min-w-0">
                <h3 class="text-lg/7 font-semibold text-gray-900 dark:text-white">
                  Activate
                  <code class="rounded bg-white/70 px-1.5 py-0.5 font-mono text-base dark:bg-black/30">Project</code>
                  as a cost allocation tag in the payer account
                </h3>
                <p class="mt-2 max-w-3xl text-sm/6 text-gray-700 dark:text-gray-300">
                  Until then these figures cover the
                  <strong>whole AWS account</strong>, not just this deployment.
                  An account is not an application — anything else deployed
                  here, another environment of this stack, another team's
                  database, an unrelated service, is being counted in the
                  infrastructure total and in cost per user.
                </p>

                <dl class="mt-4 space-y-1.5 text-sm/6 text-gray-700 dark:text-gray-300">
                  <div class="flex gap-2">
                    <dt class="shrink-0 font-medium text-gray-900 dark:text-white">Where</dt>
                    <dd>
                      AWS Billing → Cost allocation tags, in the
                      <strong>payer</strong> account. An account inside an
                      Organization cannot do this itself.
                    </dd>
                  </div>
                  @if (summary()!.projectTag) {
                    <div class="flex gap-2">
                      <dt class="shrink-0 font-medium text-gray-900 dark:text-white">Tag</dt>
                      <dd>
                        <code class="rounded bg-white/70 px-1.5 py-0.5 font-mono text-xs dark:bg-black/30">Project</code>
                        =
                        <code class="rounded bg-white/70 px-1.5 py-0.5 font-mono text-xs dark:bg-black/30">{{ summary()!.projectTag }}</code>
                        — already on every resource this stack creates. Nothing
                        to configure here.
                      </dd>
                    </div>
                  }
                  <div class="flex gap-2">
                    <dt class="shrink-0 font-medium text-gray-900 dark:text-white">Then</dt>
                    <dd>
                      The nightly sync picks it up on its own — no redeploy.
                      Activation is <strong>not retroactive</strong>, so months
                      before it stay account-wide.
                    </dd>
                  </div>
                </dl>
              </div>
            </div>
          </div>
        }

        <!-- 1. Where the money goes -->
        <div
          class="rounded-2xl border border-gray-200 bg-white shadow-xs dark:border-gray-700 dark:bg-gray-800"
        >
          <div class="border-b border-gray-200 px-6 py-4 dark:border-gray-700">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h3 class="text-lg/7 font-semibold text-gray-900 dark:text-white">
                  All-in Platform Cost
                </h3>
                <p class="mt-1 max-w-2xl text-sm/6 text-gray-500 dark:text-gray-400">
                  Inference from our own per-user ledger, infrastructure from
                  AWS Cost Explorer.
                  @if (scopedToDeployment()) {
                    Infrastructure is filtered to this deployment's own
                    resources.
                  } @else {
                    <!-- No "see below": the call to action sits directly
                         above this card, and the badge repeats the scope. -->
                    <strong>Infrastructure covers the whole AWS account</strong>,
                    not just this deployment.
                  }
                </p>
              </div>
              <div class="flex shrink-0 flex-wrap items-center gap-2">
                <!-- Scope first: it changes what every number below MEANS,
                     where "month to date" only changes how much of one. -->
                <span
                  class="rounded-full px-3 py-1 text-xs/5 font-medium"
                  [class]="
                    scopedToDeployment()
                      ? 'bg-gray-100 text-primary-accessible dark:bg-gray-700 dark:text-primary-50'
                      : 'border border-state-warning-300 text-state-warning-700 dark:border-state-warning-700 dark:text-state-warning-400'
                  "
                >
                  {{ scopedToDeployment() ? 'This deployment' : 'Whole account' }}
                </span>
                @if (summary()!.partialMonth) {
                  <span
                    class="rounded-full bg-gray-100 px-3 py-1 text-xs/5 font-medium text-gray-600 dark:bg-gray-700 dark:text-gray-300"
                  >
                    Month to date
                  </span>
                }
              </div>
            </div>
          </div>

          <dl
            class="grid grid-cols-2 divide-y divide-gray-200 lg:grid-cols-4 lg:divide-y-0 lg:divide-x dark:divide-gray-700"
          >
            <div class="px-6 py-5">
              <dt class="text-sm/6 text-gray-500 dark:text-gray-400">Total</dt>
              <dd
                class="mt-0.5 text-2xl/8 font-semibold text-gray-900 tabular-nums dark:text-white"
              >
                {{ formatCurrency(summary()!.totalCost) }}
              </dd>
            </div>
            <div class="px-6 py-5">
              <dt class="text-sm/6 text-gray-500 dark:text-gray-400">Inference</dt>
              <dd
                class="mt-0.5 text-2xl/8 font-semibold text-gray-900 tabular-nums dark:text-white"
              >
                {{ formatCurrency(summary()!.inferenceCost) }}
              </dd>
              <dd class="mt-0.5 text-xs/5 text-gray-500 tabular-nums dark:text-gray-400">
                {{ inferenceSharePercent() | number: '1.1-1' }}% of total
              </dd>
            </div>
            <div class="px-6 py-5">
              <dt class="text-sm/6 text-gray-500 dark:text-gray-400">
                Infrastructure
              </dt>
              <dd
                class="mt-0.5 text-2xl/8 font-semibold text-gray-900 tabular-nums dark:text-white"
              >
                {{ formatCurrency(summary()!.platformCost) }}
              </dd>
              <dd class="mt-0.5 text-xs/5 text-gray-500 tabular-nums dark:text-gray-400">
                {{ summary()!.platformSharePercent | number: '1.1-1' }}% of total
              </dd>
            </div>
            <div class="px-6 py-5">
              <dt class="text-sm/6 text-gray-500 dark:text-gray-400">
                Cost / user / month
              </dt>
              <dd
                class="mt-0.5 text-2xl/8 font-semibold text-gray-900 tabular-nums dark:text-white"
              >
                {{ formatCurrency(summary()!.costPerUser, 2) }}
              </dd>
              <dd class="mt-0.5 text-xs/5 text-gray-500 tabular-nums dark:text-gray-400">
                {{ formatCurrency(summary()!.inferenceCostPerUser, 2) }} inference
                + {{ formatCurrency(summary()!.platformCostPerUser, 2) }} platform
                · {{ summary()!.activeUsers | number }} users
              </dd>
            </div>
          </dl>
        </div>

        <!-- 2. Reconciliation: our pricing tables vs what AWS billed -->
        <div
          class="rounded-2xl border border-gray-200 bg-white p-6 shadow-xs dark:border-gray-700 dark:bg-gray-800"
        >
          <div class="flex items-start gap-3">
            <!-- Colour on the wrapper, not the glyph — see the note above.
                 -700/dark:-400, not -600: as the only visual carrier of
                 ok-vs-warning this wants margin over the 3:1 non-text bar,
                 and -600 measured exactly 3.20 on white. -700 measures 5.03
                 light / 10.30 dark. -700 is a LIGHT-only step — pairing it
                 with itself in dark drops to 3.53. -->
            <span
              class="mt-0.5 shrink-0"
              [class]="
                reconciliationOk()
                  ? 'text-state-success-700 dark:text-state-success-400'
                  : 'text-state-warning-700 dark:text-state-warning-400'
              "
            >
              <ng-icon
                [name]="reconciliationOk() ? 'heroCheckCircle' : 'heroExclamationTriangle'"
                size="1.25rem"
                aria-hidden="true"
              />
            </span>
            <div class="min-w-0">
              <h3 class="text-base/7 font-semibold text-gray-900 dark:text-white">
                Pricing reconciliation
                <span class="ml-1 font-normal text-gray-500 tabular-nums dark:text-gray-400">
                  {{ absDeltaPercent() | number: '1.2-2' }}% apart
                </span>
              </h3>
              <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-300">
                Our ledger charged
                <span class="font-medium tabular-nums">{{ formatCurrency(summary()!.inferenceCost) }}</span>
                for inference; AWS billed
                <span class="font-medium tabular-nums">{{ formatCurrency(summary()!.ceInferenceCost) }}</span>
                for the same model SKUs — a
                <span class="font-medium tabular-nums">{{ formatCurrency(summary()!.reconciliationDelta) }}</span>
                difference.
              </p>
              <p class="mt-2 max-w-3xl text-xs/5 text-gray-500 dark:text-gray-400">
                @if (reconciliationOk()) {
                  Within tolerance — the per-model rates in
                  <code class="font-mono">curated-models.ts</code> match what AWS
                  actually charged. This figure is a regression test on those
                  rates, not an input to any total.
                } @else {
                  Outside the 2% tolerance.
                  @if (!scopedToDeployment()) {
                    Check scope first: the AWS figure is
                    <strong>account-wide</strong>, so another deployment here
                    inflates it and no amount of correct pricing will close
                    the gap. Otherwise —
                  }
                  a model's rate in
                  <code class="font-mono">curated-models.ts</code> may have
                  drifted from its AWS model card, or spend landed on a model
                  the catalog does not price.
                }
              </p>
            </div>
          </div>
        </div>

        <!-- 3. Per-service breakdown -->
        <div
          class="overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-xs dark:border-gray-700 dark:bg-gray-800"
        >
          <div
            class="flex flex-col gap-3 border-b border-gray-200 px-6 py-4 sm:flex-row sm:items-center sm:justify-between dark:border-gray-700"
          >
            <div>
              <h3 class="text-lg/7 font-semibold text-gray-900 dark:text-white">
                Infrastructure by Service
              </h3>
              <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
                {{ platformServices().length }} services ·
                {{ formatCurrency(summary()!.platformCost) }} total
              </p>
            </div>
            @if (summary()!.syncedAt) {
              <p class="shrink-0 text-xs/5 text-gray-500 dark:text-gray-400">
                Synced {{ formatTimestamp(summary()!.syncedAt) }}
              </p>
            }
          </div>

          @if (platformServices().length === 0) {
            <div class="px-6 py-8 text-center text-sm text-gray-500 dark:text-gray-400">
              No infrastructure cost recorded for this period.
            </div>
          } @else {
            <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
              <thead class="bg-gray-50 dark:bg-gray-900">
                <tr>
                  <th
                    scope="col"
                    class="px-6 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400"
                  >
                    Service
                  </th>
                  <th
                    scope="col"
                    class="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400"
                  >
                    Cost
                  </th>
                  <th
                    scope="col"
                    class="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400"
                  >
                    Share
                  </th>
                  <th
                    scope="col"
                    class="hidden px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500 sm:table-cell dark:text-gray-400"
                  >
                    Per user
                  </th>
                </tr>
              </thead>
              <tbody class="divide-y divide-gray-200 dark:divide-gray-700">
                @for (svc of platformServices(); track svc.serviceName; let i = $index) {
                  <tr class="transition-colors hover:bg-gray-50 dark:hover:bg-gray-700/50">
                    <td class="px-6 py-3">
                      <div class="flex items-center gap-3">
                        <span
                          class="size-2.5 shrink-0 rounded-full"
                          [style.background-color]="getColor(i)"
                        ></span>
                        <span class="text-sm/6 font-medium text-gray-900 dark:text-white">
                          {{ svc.serviceName }}
                        </span>
                      </div>
                    </td>
                    <td
                      class="px-4 py-3 text-right text-sm/6 font-semibold text-gray-900 tabular-nums dark:text-white"
                    >
                      {{ formatCurrency(svc.cost) }}
                    </td>
                    <td class="px-4 py-3">
                      <div class="flex items-center justify-end gap-3">
                        <div
                          class="hidden h-1.5 w-20 overflow-hidden rounded-full bg-gray-100 lg:block dark:bg-gray-700"
                          aria-hidden="true"
                        >
                          <div
                            class="h-full rounded-full"
                            [style.width.%]="svc.percentageOfPlatform"
                            [style.background-color]="getColor(i)"
                          ></div>
                        </div>
                        <span
                          class="w-12 text-right text-sm/6 text-gray-600 tabular-nums dark:text-gray-300"
                        >
                          {{ svc.percentageOfPlatform | number: '1.1-1' }}%
                        </span>
                      </div>
                    </td>
                    <td
                      class="hidden px-4 py-3 text-right text-sm/6 text-gray-500 tabular-nums sm:table-cell dark:text-gray-400"
                    >
                      {{ perUser(svc) }}
                    </td>
                  </tr>
                }
              </tbody>
            </table>
          }

          <!-- What was held out, and why. Shown rather than silently dropped:
               the platform total is only trustworthy if an operator can see
               what it excludes. -->
          @if (excludedServices().length > 0) {
            <div
              class="border-t border-gray-200 bg-gray-50 px-6 py-4 dark:border-gray-700 dark:bg-gray-900/40"
            >
              <p class="text-xs/5 font-medium text-gray-600 dark:text-gray-300">
                Excluded from the total ({{ formatCurrency(summary()!.excludedCost) }})
              </p>
              <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                Resources this platform does not own, plus account-level
                charges no single application causes — another team's database,
                AWS Support (a percentage of spend), and the org's security
                baseline.
              </p>
              <ul role="list" class="mt-2 flex flex-wrap gap-x-4 gap-y-1">
                @for (svc of excludedServices(); track svc.serviceName) {
                  <li class="text-xs/5 text-gray-500 tabular-nums dark:text-gray-400">
                    {{ svc.serviceName }}
                    <span class="font-medium">{{ formatCurrency(svc.cost) }}</span>
                  </li>
                }
              </ul>
            </div>
          }
        </div>
      </div>
    }
  `,
})
export class PlatformCostBreakdownComponent {
  summary = input.required<PlatformCostSummary | null>();

  /** Tolerance for the pricing reconciliation. It measured 0.50% on prod. */
  private static readonly RECONCILIATION_TOLERANCE_PERCENT = 2;

  protected readonly platformServices = computed(() =>
    (this.summary()?.services ?? []).filter(s => s.category === 'platform'),
  );

  protected readonly excludedServices = computed(() =>
    (this.summary()?.services ?? []).filter(s => s.category === 'excluded'),
  );

  protected readonly inferenceSharePercent = computed(() => {
    const s = this.summary();
    if (!s || s.totalCost <= 0) return 0;
    return (s.inferenceCost / s.totalCost) * 100;
  });

  protected readonly scopedToDeployment = computed(
    () => this.summary()?.scope === 'deployment',
  );

  protected readonly absDeltaPercent = computed(() =>
    Math.abs(this.summary()?.reconciliationDeltaPercent ?? 0),
  );

  protected readonly reconciliationOk = computed(
    () =>
      this.absDeltaPercent() <=
      PlatformCostBreakdownComponent.RECONCILIATION_TOLERANCE_PERCENT,
  );

  protected getColor(index: number): string {
    return getCategoricalColor(index);
  }

  /**
   * A service's cost spread over the period's active users.
   *
   * Sub-cent for most lines, so this shows more precision than the dollar
   * columns — rounding CloudWatch's $0.04/user to $0.04 and NAT's to $0.05
   * would make two very different lines look identical.
   */
  protected perUser(svc: PlatformServiceCost): string {
    const users = this.summary()?.activeUsers ?? 0;
    if (users <= 0) return '—';
    const value = svc.cost / users;
    if (value > 0 && value < 0.01) return '<$0.01';
    return this.formatCurrency(value, 2);
  }

  protected formatCurrency(value: number, minimumFractionDigits = 2): string {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits,
      maximumFractionDigits: 2,
    }).format(value);
  }

  protected formatTimestamp(value: string | null): string {
    if (!value) return '—';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return '—';
    return parsed.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    });
  }
}
