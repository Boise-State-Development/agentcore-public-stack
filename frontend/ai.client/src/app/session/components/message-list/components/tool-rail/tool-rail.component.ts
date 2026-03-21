import {
  Component,
  input,
  signal,
  computed,
  ChangeDetectionStrategy,
} from '@angular/core';
import { KeyValuePipe } from '@angular/common';
import { JsonSyntaxHighlightPipe } from '../tool-use/json-syntax-highlight.pipe';
import { ToolCallGroup, ToolCallDisplay } from './tool-rail.model';
import { ToolResultContent } from '../../../../services/models/message.model';

@Component({
  selector: 'app-tool-rail',
  templateUrl: './tool-rail.component.html',
  styleUrl: './tool-rail.component.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [JsonSyntaxHighlightPipe, KeyValuePipe],
})
export class ToolRailComponent {
  /** The grouped tool calls to display */
  group = input.required<ToolCallGroup>();

  /** Whether the rail is expanded */
  isExpanded = signal(false);

  /** Track which individual tool results are fully expanded (for long results in fallback mode) */
  expandedResultIds = signal<Set<string>>(new Set());

  /** Max tool names shown in the collapsed header before truncating */
  private readonly COLLAPSED_MAX = 3;

  /** Determine display mode: true if any summary text exists */
  hasSummaries = computed(() =>
    !!this.group().groupSummary || this.group().calls.some(c => c.summary)
  );

  /** Tool calls visible in the collapsed header (first N) */
  collapsedHeaderCalls = computed(() =>
    this.group().calls.slice(0, this.COLLAPSED_MAX)
  );

  /** Number of tool calls beyond the collapsed limit */
  overflowCount = computed(() =>
    Math.max(0, this.group().calls.length - this.COLLAPSED_MAX)
  );

  /** Auto-expand if any tool is still pending */
  shouldAutoExpand = computed(() =>
    this.group().calls.some(c => c.status === 'pending')
  );

  /** Effective expanded state: auto-expand when tools are running */
  effectiveExpanded = computed(() =>
    this.isExpanded() || this.shouldAutoExpand()
  );

  /** Toggle rail expand/collapse */
  toggleExpanded(): void {
    this.isExpanded.update(v => !v);
  }

  /** Toggle full result display for a specific tool call */
  toggleFullResult(callId: string): void {
    this.expandedResultIds.update(ids => {
      const next = new Set(ids);
      if (next.has(callId)) {
        next.delete(callId);
      } else {
        next.add(callId);
      }
      return next;
    });
  }

  /** Check if a tool call's result is fully expanded */
  isResultExpanded(callId: string): boolean {
    return this.expandedResultIds().has(callId);
  }

  /** CSS class for status dot */
  statusDotClass(call: ToolCallDisplay): string {
    switch (call.status) {
      case 'complete': return 'status-dot bg-green-500';
      case 'pending':  return 'status-dot bg-amber-400 shimmer';
      case 'error':    return 'status-dot bg-red-500';
    }
  }

  /** Format duration for display */
  formatDuration(ms: number): string {
    return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
  }

  /** Compact one-line display of tool input params */
  formatInput(inputObj: Record<string, unknown>): string {
    return Object.entries(inputObj)
      .map(([k, v]) => `${k}: ${JSON.stringify(v)}`)
      .join(', ');
  }

  /** Get combined text from result content array, for truncation */
  getResultText(call: ToolCallDisplay): string {
    if (!call.result?.content) return '';
    return call.result.content
      .map(item => {
        if (item.text) return item.text;
        if (item.json) return JSON.stringify(item.json, null, 2);
        if (item.image) return '[image]';
        return '';
      })
      .filter(Boolean)
      .join('\n');
  }

  /** Truncate result text for collapsed display */
  truncateResult(text: string, maxLen = 200): string {
    if (text.length <= maxLen) return text;
    return text.substring(0, maxLen) + '...';
  }

  /** Get image items from result content */
  getResultImages(call: ToolCallDisplay): ToolResultContent[] {
    return call.result?.content?.filter(item => item.image) ?? [];
  }

  /** Build image data URL */
  getImageDataUrl(item: ToolResultContent): string {
    if (!item.image) return '';
    return `data:image/${item.image.format};base64,${item.image.data}`;
  }

  /** Format result content item for display */
  formatResultContent(item: ToolResultContent): string {
    if (item.text) return item.text;
    if (item.json) return JSON.stringify(item.json, null, 2);
    return '';
  }
}
