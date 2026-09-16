import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { ContextTrajectoryPoint } from '../models';
import {
  formatTokensShort,
  legendFor,
  scaleTrajectory,
  trajectoryFillClass,
} from '../pages/session-profile.util';

/**
 * Context occupancy per model call, read against the compaction threshold.
 *
 * One column per call (input + cacheRead + cacheWrite — true context
 * occupancy), colored by the call's cache status so the *shape* of a
 * conversation's spend is visible at a glance: a staircase of green hits is
 * healthy; a plateau above the threshold line painted partial-miss is the
 * spiral. The threshold is always in view because it is what the reader is
 * judging against; the context window is drawn only when it would not
 * flatten the bars, and is named in the caption otherwise.
 *
 * Marks follow the house chart rules: columns capped at 24px with a 2px
 * surface gap, hairline gridlines, a legend whenever more than one status is
 * present, text in text tokens (never the series color). The calls table on
 * the same page is the table view; each bar also carries an SVG title so the
 * numbers are reachable on hover and by assistive tech.
 */
@Component({
  selector: 'app-context-trajectory-chart',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block' },
  template: `
    @if (scale().bars.length === 0) {
      <p class="text-sm/6 text-gray-500 dark:text-gray-400">No model calls recorded.</p>
    } @else {
      <figure>
        <svg
          [attr.viewBox]="'0 0 ' + width + ' ' + height"
          class="h-48 w-full"
          role="img"
          [attr.aria-label]="ariaLabel()"
        >
          <!-- gridlines + y ticks -->
          @for (tick of scale().ticks; track tick) {
            <line
              [attr.x1]="padLeft"
              [attr.x2]="width - padRight"
              [attr.y1]="yFor(tick / scale().yMax)"
              [attr.y2]="yFor(tick / scale().yMax)"
              class="stroke-gray-200 dark:stroke-gray-700"
              stroke-width="1"
            />
            <text
              [attr.x]="padLeft - 6"
              [attr.y]="yFor(tick / scale().yMax) + 3"
              text-anchor="end"
              class="fill-gray-500 text-[10px] dark:fill-gray-400"
            >
              {{ tokens(tick) }}
            </text>
          }

          <!-- bars -->
          @for (bar of scale().bars; track bar.point.callIndex) {
            <rect
              [attr.x]="xFor(bar.point.callIndex)"
              [attr.y]="yFor(bar.height)"
              [attr.width]="barWidth()"
              [attr.height]="plotHeight * bar.height"
              rx="3"
              [class]="fillClass(bar.point)"
            >
              <title>{{ barTitle(bar.point) }}</title>
            </rect>
          }

          <!-- compaction threshold -->
          @if (scale().thresholdY; as ty) {
            <line
              [attr.x1]="padLeft"
              [attr.x2]="width - padRight"
              [attr.y1]="yFor(ty)"
              [attr.y2]="yFor(ty)"
              class="stroke-state-danger-500"
              stroke-width="1.5"
              stroke-dasharray="4 3"
            />
            <text
              [attr.x]="width - padRight"
              [attr.y]="yFor(ty) - 4"
              text-anchor="end"
              class="fill-gray-600 text-[10px] font-medium dark:fill-gray-300"
            >
              compaction threshold {{ tokens(threshold()) }}
            </text>
          }

          <!-- context window, only when it fits -->
          @if (scale().windowY; as wy) {
            <line
              [attr.x1]="padLeft"
              [attr.x2]="width - padRight"
              [attr.y1]="yFor(wy)"
              [attr.y2]="yFor(wy)"
              class="stroke-gray-400 dark:stroke-gray-500"
              stroke-width="1"
              stroke-dasharray="2 3"
            />
            <text
              [attr.x]="width - padRight"
              [attr.y]="yFor(wy) - 4"
              text-anchor="end"
              class="fill-gray-500 text-[10px] dark:fill-gray-400"
            >
              context window {{ tokens(contextWindow() ?? 0) }}
            </text>
          }

          <!-- baseline -->
          <line
            [attr.x1]="padLeft"
            [attr.x2]="width - padRight"
            [attr.y1]="yFor(0)"
            [attr.y2]="yFor(0)"
            class="stroke-gray-300 dark:stroke-gray-600"
            stroke-width="1"
          />
        </svg>

        <figcaption class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs/5 text-gray-500 dark:text-gray-400">
          <span>{{ scale().bars.length }} {{ scale().bars.length === 1 ? 'call' : 'calls' }}, left to right</span>
          @if (!scale().windowY && contextWindow()) {
            <span>window {{ tokens(contextWindow() ?? 0) }}</span>
          }
          @if (legend().length > 1) {
            <ul class="flex flex-wrap items-center gap-x-3 gap-y-1" aria-label="Cache status legend">
              @for (entry of legend(); track entry.status) {
                <li class="inline-flex items-center gap-1.5">
                  <svg class="size-2.5" viewBox="0 0 10 10" aria-hidden="true">
                    <rect width="10" height="10" rx="2" [class]="fillClass({ cacheStatus: entry.status })" />
                  </svg>
                  {{ entry.label }}
                </li>
              }
            </ul>
          }
        </figcaption>
      </figure>
    }
  `,
})
export class ContextTrajectoryChartComponent {
  readonly points = input.required<ContextTrajectoryPoint[]>();
  readonly threshold = input.required<number>();
  readonly contextWindow = input<number | null | undefined>(null);

  // Fixed drawing frame; the SVG scales to its container via viewBox.
  readonly width = 720;
  readonly height = 192;
  readonly padLeft = 44;
  readonly padRight = 8;
  readonly padTop = 18;
  readonly padBottom = 8;
  readonly plotHeight = this.height - this.padTop - this.padBottom;

  readonly scale = computed(() => scaleTrajectory(this.points(), this.threshold(), this.contextWindow()));
  readonly legend = computed(() => legendFor(this.points()));

  /** Column width: fill the slot up to the 24px cap, leaving a 2px surface gap. */
  readonly barWidth = computed(() => {
    const n = Math.max(1, this.scale().bars.length);
    const slot = (this.width - this.padLeft - this.padRight) / n;
    return Math.max(2, Math.min(24, slot - 2));
  });

  readonly ariaLabel = computed(() => {
    const s = this.scale();
    const peak = s.bars.reduce((m, b) => Math.max(m, b.point.contextTokens), 0);
    return `Context tokens per model call: ${s.bars.length} calls, peak ${formatTokensShort(peak)}, compaction threshold ${formatTokensShort(this.threshold())}.`;
  });

  xFor(callIndex: number): number {
    const n = Math.max(1, this.scale().bars.length);
    const slot = (this.width - this.padLeft - this.padRight) / n;
    // Center the column in its slot so the gap is symmetric.
    return this.padLeft + callIndex * slot + (slot - this.barWidth()) / 2;
  }

  /** Map a 0..1 share of the plot to a y pixel (SVG y grows downward). */
  yFor(share: number): number {
    return this.padTop + this.plotHeight * (1 - Math.max(0, Math.min(1, share)));
  }

  tokens(value: number): string {
    return formatTokensShort(Math.round(value));
  }

  fillClass(point: Pick<ContextTrajectoryPoint, 'cacheStatus'>): string {
    return trajectoryFillClass(point.cacheStatus);
  }

  barTitle(point: ContextTrajectoryPoint): string {
    const parts = [
      `Call ${point.callIndex + 1}: ${new Intl.NumberFormat('en-US').format(point.contextTokens)} tokens in context`,
      point.cacheStatus ? `cache: ${point.cacheStatus}` : null,
      point.modelId ? `model: ${point.modelId}` : null,
      point.cost != null ? `cost: $${point.cost.toFixed(4)}` : null,
    ];
    return parts.filter(Boolean).join(' · ');
  }
}
