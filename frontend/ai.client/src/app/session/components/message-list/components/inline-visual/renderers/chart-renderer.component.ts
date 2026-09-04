import {
  Component,
  input,
  output,
  viewChild,
  effect,
  ElementRef,
  ChangeDetectionStrategy,
  OnDestroy
} from '@angular/core';
import { Chart, ChartConfiguration, ChartType, ChartData, ChartOptions } from 'chart.js/auto';
import { getChromeColorsForMode } from '../../../../../../shared/constants/chart-colors.constants';

/**
 * Payload structure for Chart.js charts.
 * Matches the backend visualization tool output.
 */
export interface ChartPayload {
  chartType: 'bar' | 'line' | 'pie' | 'doughnut' | 'polarArea' | 'radar' | 'scatter' | 'bubble';
  title?: string;
  data: ChartData;
  options?: ChartOptions;
}

/**
 * Renders Chart.js charts inline in the conversation.
 * Supports bar, line, pie, and doughnut chart types.
 */
@Component({
  selector: 'app-chart-renderer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="chart-container rounded-lg border border-gray-200 dark:border-gray-700
                bg-white dark:bg-gray-800 overflow-hidden">
      <!-- Header -->
      <div class="flex items-center justify-between px-4 py-3 border-b border-gray-200 dark:border-gray-700">
        @if (chartPayload()?.title) {
          <h4 class="text-sm font-medium text-gray-900 dark:text-white">
            {{ chartPayload()!.title }}
          </h4>
        } @else {
          <span></span>
        }

        <div class="flex items-center gap-1">
          <button
            type="button"
            (click)="toggleExpanded.emit()"
            class="p-1.5 text-gray-500 hover:text-gray-700 dark:text-gray-400
                   dark:hover:text-gray-200 rounded-md hover:bg-gray-100 dark:hover:bg-gray-700"
            [attr.aria-label]="isExpanded() ? 'Collapse chart' : 'Expand chart'"
          >
            <!-- Expand/Collapse icon -->
            <svg class="size-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              @if (isExpanded()) {
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                      d="M5 15l7-7 7 7" />
              } @else {
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                      d="M19 9l-7 7-7-7" />
              }
            </svg>
          </button>

          <button
            type="button"
            (click)="dismiss.emit()"
            class="p-1.5 text-gray-500 hover:text-gray-700 dark:text-gray-400
                   dark:hover:text-gray-200 rounded-md hover:bg-gray-100 dark:hover:bg-gray-700"
            aria-label="Dismiss chart"
          >
            <svg class="size-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                    d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      </div>

      <!-- Chart -->
      @if (isExpanded()) {
        <div class="p-4">
          <div class="h-64">
            <canvas #chartCanvas></canvas>
          </div>
        </div>
      }
    </div>
  `,
  styles: `
    .chart-container {
      width: 100%;
    }
  `
})
export class ChartRendererComponent implements OnDestroy {
  /** The chart payload from the backend */
  payload = input.required<unknown>();

  /** Whether the chart is expanded */
  isExpanded = input<boolean>(true);

  /** Emitted when user dismisses the chart */
  dismiss = output<void>();

  /** Emitted when user toggles expand/collapse */
  toggleExpanded = output<void>();

  /** Reference to the canvas element */
  private chartCanvas = viewChild<ElementRef<HTMLCanvasElement>>('chartCanvas');

  /** The Chart.js instance */
  private chart: Chart | null = null;

  constructor() {
    // Render chart when canvas is available and payload changes
    effect(() => {
      const canvas = this.chartCanvas();
      const payload = this.chartPayload();
      const expanded = this.isExpanded();

      if (canvas && payload && expanded) {
        // Small delay to ensure DOM is ready
        setTimeout(() => this.renderChart(canvas.nativeElement, payload), 0);
      }
    });
  }

  ngOnDestroy(): void {
    this.destroyChart();
  }

  /** Parse and validate the payload */
  chartPayload(): ChartPayload | null {
    const raw = this.payload();
    if (!raw || typeof raw !== 'object') return null;

    const payload = raw as ChartPayload;
    if (!payload.chartType || !payload.data) return null;

    return payload;
  }

  /** Render the chart on the canvas */
  private renderChart(canvas: HTMLCanvasElement, payload: ChartPayload): void {
    // Destroy existing chart if present
    this.destroyChart();

    // Detect dark mode
    const isDarkMode = document.documentElement.classList.contains('dark');

    // Merge default options with payload options
    const options = this.buildOptions(payload, isDarkMode);

    const config: ChartConfiguration = {
      type: payload.chartType as ChartType,
      data: payload.data,
      options
    };

    try {
      this.chart = new Chart(canvas, config);
    } catch (error) {
      console.error('Failed to render chart:', error);
    }
  }

  /** Build chart options with theme-aware colors */
  private buildOptions(payload: ChartPayload, isDarkMode: boolean): ChartOptions {
    const chromeColors = getChromeColorsForMode(isDarkMode);

    const baseOptions: ChartOptions = {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          labels: { color: chromeColors.axisText }
        },
        title: {
          color: chromeColors.axisText
        },
        tooltip: {
          backgroundColor: chromeColors.background,
          titleColor: chromeColors.titleText,
          bodyColor: chromeColors.bodyText,
          borderColor: chromeColors.border,
          borderWidth: 1
        }
      }
    };

    // Add scales for cartesian charts (bar, line, scatter, bubble)
    if (['bar', 'line', 'scatter', 'bubble'].includes(payload.chartType)) {
      baseOptions.scales = {
        x: {
          ticks: { color: chromeColors.axisText },
          grid: { color: chromeColors.gridLine }
        },
        y: {
          ticks: { color: chromeColors.axisText },
          grid: { color: chromeColors.gridLine },
          beginAtZero: true
        }
      };
    }

    // Add radial scale for radar charts
    if (payload.chartType === 'radar') {
      baseOptions.scales = {
        r: {
          ticks: { color: chromeColors.axisText, backdropColor: 'transparent' },
          grid: { color: chromeColors.gridLine },
          pointLabels: { color: chromeColors.axisText },
          beginAtZero: true
        }
      };
    }

    // Deep merge with payload options
    return this.deepMerge(baseOptions, payload.options || {});
  }

  /** Deep merge two objects */
  private deepMerge(target: Record<string, unknown>, source: Record<string, unknown>): ChartOptions {
    const result = { ...target };
    for (const key in source) {
      if (
        source[key] instanceof Object &&
        !Array.isArray(source[key]) &&
        key in target &&
        target[key] instanceof Object
      ) {
        result[key] = this.deepMerge(
          target[key] as Record<string, unknown>,
          source[key] as Record<string, unknown>
        );
      } else {
        result[key] = source[key];
      }
    }
    return result as ChartOptions;
  }

  /** Destroy the current chart instance */
  private destroyChart(): void {
    if (this.chart) {
      this.chart.destroy();
      this.chart = null;
    }
  }
}
