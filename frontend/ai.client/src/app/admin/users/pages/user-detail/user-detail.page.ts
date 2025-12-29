import {
  Component,
  ChangeDetectionStrategy,
  inject,
  OnInit,
  computed,
} from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroUser,
  heroCurrencyDollar,
  heroChartBar,
  heroShieldCheck,
  heroExclamationTriangle,
  heroClock,
} from '@ng-icons/heroicons/outline';
import { UserStateService } from '../../services/user-state.service';
import { QuotaEventSummary } from '../../models';

@Component({
  selector: 'app-user-detail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroUser,
      heroCurrencyDollar,
      heroChartBar,
      heroShieldCheck,
      heroExclamationTriangle,
      heroClock,
    }),
  ],
  host: {
    class: 'block p-6',
  },
  template: `
    <!-- Back Button -->
    <button
      (click)="goBack()"
      class="flex items-center gap-2 text-gray-600 hover:text-gray-900 mb-6 dark:text-gray-400 dark:hover:text-white"
    >
      <ng-icon name="heroArrowLeft" class="size-5" />
      <span>Back to Users</span>
    </button>

    <!-- Error State -->
    @if (state.hasError()) {
      <div class="mb-6 p-4 bg-red-50 border border-red-200 rounded-sm text-red-800 dark:bg-red-900/20 dark:border-red-800 dark:text-red-200">
        <p>{{ state.error() }}</p>
        <button
          (click)="state.clearError()"
          class="mt-2 text-sm underline hover:no-underline"
        >
          Dismiss
        </button>
      </div>
    }

    @if (state.loading()) {
      <div class="flex items-center justify-center h-64">
        <div class="flex flex-col items-center gap-4">
          <div
            class="animate-spin rounded-full size-12 border-4 border-gray-300 dark:border-gray-600 border-t-blue-600"
          ></div>
          <p class="text-sm text-gray-500 dark:text-gray-400">
            Loading user details...
          </p>
        </div>
      </div>
    }

    @if (user(); as detail) {
      <!-- Profile Header -->
      <div
        class="flex items-start gap-6 p-6 bg-white border border-gray-200 rounded-sm mb-6 dark:bg-gray-800 dark:border-gray-700"
      >
        <!-- Avatar -->
        @if (detail.profile.picture) {
          <img
            [src]="detail.profile.picture"
            [alt]="detail.profile.name"
            class="size-16 rounded-full"
          />
        } @else {
          <div
            class="flex items-center justify-center size-16 rounded-full bg-gray-200 dark:bg-gray-700"
          >
            <ng-icon name="heroUser" class="size-8 text-gray-500" />
          </div>
        }

        <!-- Info -->
        <div class="flex-1">
          <h1 class="text-2xl/9 font-bold">{{ detail.profile.name || 'Unknown User' }}</h1>
          <p class="text-gray-600 dark:text-gray-400">{{ detail.profile.email }}</p>
          <div class="flex items-center gap-4 mt-2 text-sm/6">
            <span class="text-gray-500">ID: {{ detail.profile.userId }}</span>
            <span class="text-gray-500">Domain: {{ detail.profile.emailDomain }}</span>
          </div>
          <div class="flex items-center gap-2 mt-2">
            @for (role of detail.profile.roles; track role) {
              <span
                class="px-2 py-0.5 text-xs bg-blue-100 text-blue-800 rounded-xs dark:bg-blue-900 dark:text-blue-200"
              >
                {{ role }}
              </span>
            }
          </div>
        </div>

        <!-- Status Badge -->
        <div>
          <span
            class="px-3 py-1 text-sm rounded-sm"
            [class]="getStatusClass(detail.profile.status)"
          >
            {{ detail.profile.status }}
          </span>
        </div>
      </div>

      <!-- Stats Grid -->
      <div class="grid gap-6 md:grid-cols-2 lg:grid-cols-3 mb-6">
        <!-- Cost Summary -->
        <div
          class="p-6 bg-white border border-gray-200 rounded-sm dark:bg-gray-800 dark:border-gray-700"
        >
          <div class="flex items-center gap-2 mb-4">
            <ng-icon name="heroCurrencyDollar" class="size-5 text-green-600" />
            <h3 class="font-semibold">Current Month Cost</h3>
          </div>
          <div class="text-3xl font-bold mb-2">
            \${{ detail.costSummary.totalCost.toFixed(2) }}
          </div>
          <div class="space-y-1 text-sm/6 text-gray-600 dark:text-gray-400">
            <div>{{ detail.costSummary.totalRequests }} requests</div>
            <div>
              {{ formatTokens(detail.costSummary.totalInputTokens) }} input /
              {{ formatTokens(detail.costSummary.totalOutputTokens) }} output tokens
            </div>
            @if (detail.costSummary.cacheSavings > 0) {
              <div class="text-green-600">
                \${{ detail.costSummary.cacheSavings.toFixed(2) }} cache savings
              </div>
            }
          </div>
        </div>

        <!-- Quota Status -->
        <div
          class="p-6 bg-white border border-gray-200 rounded-sm dark:bg-gray-800 dark:border-gray-700"
        >
          <div class="flex items-center gap-2 mb-4">
            <ng-icon name="heroChartBar" class="size-5 text-blue-600" />
            <h3 class="font-semibold">Quota Status</h3>
          </div>
          @if (detail.quotaStatus.tierName) {
            <div class="mb-2">
              <span class="text-lg font-medium">{{ detail.quotaStatus.tierName }}</span>
              <span class="text-sm/6 text-gray-500 ml-2">
                ({{ detail.quotaStatus.matchedBy }})
              </span>
            </div>
            <!-- Progress Bar -->
            <div class="mb-2">
              <div class="flex justify-between text-sm/6 mb-1">
                <span>\${{ detail.quotaStatus.currentUsage.toFixed(2) }}</span>
                <span>{{ detail.quotaStatus.monthlyLimit ? '\$' + detail.quotaStatus.monthlyLimit.toFixed(2) : '\u221E' }}</span>
              </div>
              <div class="h-2 bg-gray-200 rounded-full dark:bg-gray-700">
                <div
                  class="h-2 rounded-full transition-all"
                  [class]="getUsageBarClass(detail.quotaStatus.usagePercentage)"
                  [style.width.%]="getUsageBarWidth(detail.quotaStatus.usagePercentage)"
                ></div>
              </div>
              <div class="text-sm/6 text-gray-500 mt-1">
                {{ detail.quotaStatus.usagePercentage.toFixed(1) }}% used
                @if (detail.quotaStatus.remaining !== undefined && detail.quotaStatus.remaining !== null) {
                  &middot; \${{ detail.quotaStatus.remaining.toFixed(2) }} remaining
                }
              </div>
            </div>
            @if (detail.quotaStatus.hasActiveOverride) {
              <div
                class="flex items-center gap-2 mt-3 p-2 bg-yellow-50 border border-yellow-200 rounded-xs dark:bg-yellow-900/20 dark:border-yellow-800"
              >
                <ng-icon name="heroShieldCheck" class="size-4 text-yellow-600" />
                <span class="text-sm/6">
                  Override active: {{ detail.quotaStatus.overrideReason }}
                </span>
              </div>
            }
          } @else {
            <div class="text-gray-500">No quota assigned</div>
          }
        </div>

        <!-- Activity -->
        <div
          class="p-6 bg-white border border-gray-200 rounded-sm dark:bg-gray-800 dark:border-gray-700"
        >
          <div class="flex items-center gap-2 mb-4">
            <ng-icon name="heroClock" class="size-5 text-purple-600" />
            <h3 class="font-semibold">Activity</h3>
          </div>
          <div class="space-y-2 text-sm/6">
            <div class="flex justify-between">
              <span class="text-gray-500">Member since:</span>
              <span>{{ formatFullDate(detail.profile.createdAt) }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-500">Last login:</span>
              <span>{{ formatFullDate(detail.profile.lastLoginAt) }}</span>
            </div>
            @if (detail.costSummary.primaryModel) {
              <div class="flex justify-between">
                <span class="text-gray-500">Primary model:</span>
                <span>{{ detail.costSummary.primaryModel }}</span>
              </div>
            }
          </div>
        </div>
      </div>

      <!-- Recent Events -->
      <div
        class="p-6 bg-white border border-gray-200 rounded-sm dark:bg-gray-800 dark:border-gray-700"
      >
        <div class="flex items-center justify-between mb-4">
          <h3 class="font-semibold">Recent Quota Events</h3>
          <button
            (click)="viewAllEvents()"
            class="text-sm/6 text-blue-600 hover:text-blue-800 dark:text-blue-400"
          >
            View All
          </button>
        </div>
        @if (detail.recentEvents.length > 0) {
          <div class="space-y-3">
            @for (event of detail.recentEvents; track event.eventId) {
              <div class="flex items-center gap-3 p-3 bg-gray-50 rounded-xs dark:bg-gray-700">
                <ng-icon
                  [name]="getEventIcon(event)"
                  class="size-5"
                  [class]="getEventIconClass(event)"
                />
                <div class="flex-1">
                  <span class="font-medium capitalize">{{ event.eventType }}</span>
                  <span class="text-gray-500 ml-2">
                    at {{ event.percentageUsed.toFixed(0) }}% usage
                  </span>
                </div>
                <span class="text-sm/6 text-gray-500">
                  {{ formatFullDate(event.timestamp) }}
                </span>
              </div>
            }
          </div>
        } @else {
          <div class="text-center py-4 text-gray-500">No recent events</div>
        }
      </div>

      <!-- Admin Actions -->
      <div class="flex gap-4 mt-6">
        <button
          (click)="createOverride()"
          class="px-4 py-2 bg-blue-600 text-white rounded-sm hover:bg-blue-700"
        >
          Create Override
        </button>
        <button
          (click)="assignTier()"
          class="px-4 py-2 border border-gray-300 rounded-sm hover:bg-gray-50 dark:border-gray-600 dark:hover:bg-gray-800"
        >
          Assign Tier
        </button>
      </div>
    }
  `,
})
export class UserDetailPage implements OnInit {
  state = inject(UserStateService);
  private route = inject(ActivatedRoute);
  private router = inject(Router);

  user = computed(() => this.state.selectedUser());

  ngOnInit(): void {
    const userId = this.route.snapshot.paramMap.get('userId');
    if (userId) {
      this.state.loadUserDetail(userId);
    }
  }

  goBack(): void {
    this.state.clearSelection();
    this.router.navigate(['/admin/users']);
  }

  createOverride(): void {
    const userId = this.user()?.profile.userId;
    if (userId) {
      this.router.navigate(['/admin/quota/overrides/new'], {
        queryParams: { userId },
      });
    }
  }

  assignTier(): void {
    const userId = this.user()?.profile.userId;
    if (userId) {
      this.router.navigate(['/admin/quota/assignments/new'], {
        queryParams: { userId, type: 'direct_user' },
      });
    }
  }

  viewAllEvents(): void {
    const userId = this.user()?.profile.userId;
    if (userId) {
      this.router.navigate(['/admin/quota/events'], {
        queryParams: { userId },
      });
    }
  }

  formatTokens(tokens: number): string {
    if (tokens >= 1_000_000) {
      return `${(tokens / 1_000_000).toFixed(1)}M`;
    } else if (tokens >= 1_000) {
      return `${(tokens / 1_000).toFixed(1)}K`;
    }
    return tokens.toString();
  }

  formatFullDate(isoString: string): string {
    if (!isoString) {
      return 'Never';
    }
    const date = new Date(isoString);
    if (isNaN(date.getTime())) {
      return 'Never';
    }
    return date.toLocaleString();
  }

  getStatusClass(status: string): string {
    switch (status) {
      case 'active':
        return 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200';
      case 'suspended':
        return 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200';
      default:
        return 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200';
    }
  }

  getUsageBarClass(percentage: number): string {
    if (percentage >= 90) {
      return 'bg-red-500';
    } else if (percentage >= 80) {
      return 'bg-yellow-500';
    }
    return 'bg-green-500';
  }

  getUsageBarWidth(percentage: number): number {
    return Math.min(percentage, 100);
  }

  getEventIcon(event: QuotaEventSummary): string {
    return event.eventType === 'block' ? 'heroExclamationTriangle' : 'heroChartBar';
  }

  getEventIconClass(event: QuotaEventSummary): string {
    switch (event.eventType) {
      case 'block':
        return 'text-red-500';
      case 'warning':
        return 'text-yellow-500';
      default:
        return 'text-blue-500';
    }
  }
}
