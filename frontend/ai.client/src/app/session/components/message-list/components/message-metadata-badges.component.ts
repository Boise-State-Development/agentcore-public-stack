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
    };
    attribution?: {
        userId?: string;
        sessionId?: string;
        timestamp?: string;
    };
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

                <!-- Token Usage Badge -->
                @if (tokenInfo()) {
                    <div class="inline-flex items-center gap-1.5 rounded-full bg-purple-100 px-3 py-1 text-xs font-medium text-purple-700 dark:bg-purple-900/30 dark:text-purple-300">
                        <svg class="size-3.5" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
                            <path stroke-linecap="round" stroke-linejoin="round" d="M2.25 18.75a60.07 60.07 0 0 1 15.797 2.101c.727.198 1.453-.342 1.453-1.096V18.75M3.75 4.5v.75A.75.75 0 0 1 3 6h-.75m0 0v-.375c0-.621.504-1.125 1.125-1.125H20.25M2.25 6v9m18-10.5v.75c0 .414.336.75.75.75h.75m-1.5-1.5h.375c.621 0 1.125.504 1.125 1.125v9.75c0 .621-.504 1.125-1.125 1.125h-.375m1.5-1.5H21a.75.75 0 0 0-.75.75v.75m0 0H3.75m0 0h-.375a1.125 1.125 0 0 1-1.125-1.125V15m1.5 1.5v-.75A.75.75 0 0 0 3 15h-.75M15 10.5a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm3 0h.008v.008H18V10.5Zm-12 0h.008v.008H6V10.5Z" />
                        </svg>
                        <span>Token: {{ tokenInfo() }}</span>
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
            !!meta.tokenUsage
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

    tokenInfo = computed(() => {
        const meta = this.typedMetadata();
        const usage = meta?.tokenUsage;
        if (!usage) return null;

        const inputTokens = usage.inputTokens ?? 0;
        const outputTokens = usage.outputTokens ?? 0;
        const totalTokens = usage.totalTokens ?? (inputTokens + outputTokens);

        return `${inputTokens} in / ${outputTokens} out  (${totalTokens} write)`;
    });
}
