import {
    ChangeDetectionStrategy,
    Component,
    computed,
    input,
} from '@angular/core';

interface MessageMetadata {
    latency?: {
        timeToFirstToken?: number;
        endToEndLatency?: number;
    };
    tokenUsage?: {
        inputTokens?: number;
        outputTokens?: number;
        totalTokens?: number;
        cacheReadInputTokens?: number;
        cacheWriteInputTokens?: number;
    };
    attribution?: {
        userId?: string;
        sessionId?: string;
        timestamp?: string;
    };
    cost?: number;
}

@Component({
    selector: 'app-message-metadata-badges',
    changeDetection: ChangeDetectionStrategy.OnPush,
    imports: [],
    template: `
        @if (hasMetadata()) {
            <div class="mt-2 flex flex-wrap gap-2">
                <!-- TTFT Badge -->
                @if (ttft()) {
                    <div class="inline-flex items-center gap-1.5 rounded-full bg-blue-100 px-3 py-1 text-xs font-medium text-blue-700 dark:bg-blue-900/30 dark:text-blue-300">
                        <svg class="size-3.5" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" d="m3.75 13.5 10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75Z" />
                        </svg>
                        <span>TTFT: {{ ttft() }}ms</span>
                    </div>
                }

                <!-- E2E Badge -->
                @if (e2e()) {
                    <div class="inline-flex items-center gap-1.5 rounded-full bg-green-100 px-3 py-1 text-xs font-medium text-green-700 dark:bg-green-900/30 dark:text-green-300">
                        <svg class="size-3.5" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" />
                        </svg>
                        <span>E2E: {{ e2e() }}ms</span>
                    </div>
                }

                <!-- Input Tokens Badge -->
                @if (inputTokens() !== null) {
                    <div class="inline-flex items-center gap-1.5 rounded-full bg-indigo-100 px-3 py-1 text-xs font-medium text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-300">
                        <svg class="size-3.5" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5m-13.5-9L12 3m0 0 4.5 4.5M12 3v13.5" />
                        </svg>
                        <span>In: {{ formatNumber(inputTokens()) }}</span>
                    </div>
                }

                <!-- Output Tokens Badge -->
                @if (outputTokens() !== null) {
                    <div class="inline-flex items-center gap-1.5 rounded-full bg-purple-100 px-3 py-1 text-xs font-medium text-purple-700 dark:bg-purple-900/30 dark:text-purple-300">
                        <svg class="size-3.5" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5M16.5 12 12 16.5m0 0L7.5 12m4.5 4.5V3" />
                        </svg>
                        <span>Out: {{ formatNumber(outputTokens()) }}</span>
                    </div>
                }

                <!-- Cache Efficiency Badge (only show if there's cache activity) -->
                @if (cacheStats()) {
                    <div
                        class="inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium"
                        [class]="cacheStats()!.hitRate >= 80
                            ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300'
                            : cacheStats()!.hitRate >= 50
                                ? 'bg-cyan-100 text-cyan-700 dark:bg-cyan-900/30 dark:text-cyan-300'
                                : 'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-300'"
                    >
                        <!-- Cache/database icon -->
                        <svg class="size-3.5" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" d="M20.25 6.375c0 2.278-3.694 4.125-8.25 4.125S3.75 8.653 3.75 6.375m16.5 0c0-2.278-3.694-4.125-8.25-4.125S3.75 4.097 3.75 6.375m16.5 0v11.25c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125V6.375m16.5 0v3.75m-16.5-3.75v3.75m16.5 0v3.75C20.25 16.153 16.556 18 12 18s-8.25-1.847-8.25-4.125v-3.75m16.5 0c0 2.278-3.694 4.125-8.25 4.125s-8.25-1.847-8.25-4.125" />
                        </svg>
                        <span>Cache: {{ cacheStats()!.hitRate.toFixed(0) }}% hit</span>
                        @if (cacheStats()!.savings > 0) {
                            <span class="text-emerald-600 dark:text-emerald-400">({{ cacheStats()!.savings.toFixed(0) }}% saved)</span>
                        }
                    </div>
                }

                <!-- Cache Write Badge (show when writing to cache - first request or cache miss) -->
                @if (cacheWriteTokens() && !cacheReadTokens()) {
                    <div class="inline-flex items-center gap-1.5 rounded-full bg-yellow-100 px-3 py-1 text-xs font-medium text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-300">
                        <svg class="size-3.5" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" d="M12 10.5v6m3-3H9m4.06-7.19-2.12-2.12a1.5 1.5 0 0 0-1.061-.44H4.5A2.25 2.25 0 0 0 2.25 6v12a2.25 2.25 0 0 0 2.25 2.25h15A2.25 2.25 0 0 0 21.75 18V9a2.25 2.25 0 0 0-2.25-2.25h-5.379a1.5 1.5 0 0 1-1.06-.44Z" />
                        </svg>
                        <span>Cache Write: {{ formatNumber(cacheWriteTokens()) }}</span>
                    </div>
                }

                <!-- Cost Badge -->
                @if (cost() !== null) {
                    <div class="inline-flex items-center gap-1.5 rounded-full bg-amber-100 px-3 py-1 text-xs font-medium text-amber-700 dark:bg-amber-900/30 dark:text-amber-300">
                        <svg class="size-3.5" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" d="M12 6v12m-3-2.818.879.659c1.171.879 3.07.879 4.242 0 1.172-.879 1.172-2.303 0-3.182C13.536 12.219 12.768 12 12 12c-.725 0-1.45-.22-2.003-.659-1.106-.879-1.106-2.303 0-3.182s2.9-.879 4.006 0l.415.33M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" />
                        </svg>
                        <span>Cost: {{ formatCost(cost()) }}</span>
                    </div>
                }
            </div>
        }
    `,
    styles: `
        @import "tailwindcss";
        @custom-variant dark (&:where(.dark, .dark *));

        :host {
            display: block;
        }
    `,
})
export class MessageMetadataBadgesComponent {
    metadata = input<Record<string, unknown> | null>();

    // Computed properties to extract metadata values
    private typedMetadata = computed<MessageMetadata | null>(() => {
        const meta = this.metadata();
        if (!meta) return null;
        return meta as MessageMetadata;
    });

    hasMetadata = computed(() => {
        const meta = this.typedMetadata();
        return !!meta && (
            !!meta.latency?.timeToFirstToken ||
            !!meta.latency?.endToEndLatency ||
            !!meta.tokenUsage ||
            meta.cost !== undefined
        );
    });

    ttft = computed(() => {
        const meta = this.typedMetadata();
        return meta?.latency?.timeToFirstToken ?? null;
    });

    e2e = computed(() => {
        const meta = this.typedMetadata();
        return meta?.latency?.endToEndLatency ?? null;
    });

    inputTokens = computed(() => {
        const meta = this.typedMetadata();
        const usage = meta?.tokenUsage;
        if (!usage || usage.inputTokens === undefined) return null;
        return usage.inputTokens;
    });

    outputTokens = computed(() => {
        const meta = this.typedMetadata();
        const usage = meta?.tokenUsage;
        if (!usage || usage.outputTokens === undefined) return null;
        return usage.outputTokens;
    });

    cacheReadTokens = computed(() => {
        const meta = this.typedMetadata();
        const usage = meta?.tokenUsage;
        if (!usage) return null;
        return usage.cacheReadInputTokens ?? null;
    });

    cacheWriteTokens = computed(() => {
        const meta = this.typedMetadata();
        const usage = meta?.tokenUsage;
        if (!usage) return null;
        return usage.cacheWriteInputTokens ?? null;
    });

    /**
     * Calculate cache efficiency statistics.
     * Returns null if no cache activity, otherwise returns hit rate and cost savings.
     *
     * Cache pricing model (Bedrock):
     * - Cache read: 10% of base cost (90% savings)
     * - Cache write: 125% of base cost (25% premium)
     * - Regular input: 100% of base cost
     */
    cacheStats = computed<{ hitRate: number; savings: number } | null>(() => {
        const cacheRead = this.cacheReadTokens() ?? 0;
        const cacheWrite = this.cacheWriteTokens() ?? 0;
        const uncachedInput = this.inputTokens() ?? 0;

        // Only show cache stats if there's cache read activity (cache hits)
        if (cacheRead === 0) return null;

        // Total input tokens = cache read + cache write + uncached
        const totalInput = cacheRead + cacheWrite + uncachedInput;
        if (totalInput === 0) return null;

        // Cache hit rate: percentage of tokens served from cache
        const hitRate = (cacheRead / totalInput) * 100;

        // Calculate cost savings compared to no caching
        // Without caching: all tokens at 100% cost
        // With caching: cache_read * 0.10 + cache_write * 1.25 + uncached * 1.0
        const costWithoutCache = totalInput; // Normalized to 1.0 per token
        const costWithCache = (cacheRead * 0.10) + (cacheWrite * 1.25) + uncachedInput;
        const savings = ((costWithoutCache - costWithCache) / costWithoutCache) * 100;

        return { hitRate, savings: Math.max(0, savings) };
    });

    cost = computed(() => {
        const meta = this.typedMetadata();
        if (meta?.cost === undefined || meta.cost === null) return null;
        return meta.cost;
    });

    /**
     * Format large numbers with locale-aware thousands separators
     */
    formatNumber(value: number | null): string {
        if (value === null) return '';
        return value.toLocaleString();
    }

    /**
     * Format cost value with appropriate precision
     * Shows more decimal places for very small costs
     */
    formatCost(value: number | null): string {
        if (value === null) return '';

        // For very small costs (< $0.01), show more precision
        if (value < 0.01 && value > 0) {
            return `$${value.toFixed(6)}`;
        }
        // For small costs (< $1), show 4 decimal places
        if (value < 1) {
            return `$${value.toFixed(4)}`;
        }
        // For larger costs, show 2 decimal places
        return `$${value.toFixed(2)}`;
    }
}
