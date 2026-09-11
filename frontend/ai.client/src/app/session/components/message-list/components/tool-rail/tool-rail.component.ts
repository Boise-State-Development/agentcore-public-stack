import {
  Component,
  input,
  signal,
  computed,
  ChangeDetectionStrategy,
} from '@angular/core';
import { KeyValuePipe } from '@angular/common';
import { JsonSyntaxHighlightPipe } from '../tool-use/json-syntax-highlight.pipe';
import { ToolCallGroup, ToolCallDisplay, ToolCallBatch } from './tool-rail.model';
import { describeToolCall, describeToolGroup } from './tool-summary';
import { PinScrollToBottomDirective } from './pin-scroll-to-bottom.directive';
import { ToolResultContent } from '../../../../services/models/message.model';

@Component({
  selector: 'app-tool-rail',
  templateUrl: './tool-rail.component.html',
  styleUrl: './tool-rail.component.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [JsonSyntaxHighlightPipe, KeyValuePipe, PinScrollToBottomDirective],
})
export class ToolRailComponent {
  /** The grouped tool calls to display */
  group = input.required<ToolCallGroup>();

  /** Whether the rail is expanded */
  isExpanded = signal(false);

  /**
   * Calls whose raw input/result detail has been revealed.
   *
   * Three levels of disclosure, each answering a different question: the
   * collapsed rail says what the agent did, expanding says which steps it
   * took, and this says what each step actually sent and got back. The last
   * is reference material — a wall of JSON under every row buries the very
   * summaries that make the rail readable — so it stays folded until asked
   * for.
   */
  expandedCallIds = signal<Set<string>>(new Set());

  /** Track which individual tool results are fully expanded (for long results in fallback mode) */
  expandedResultIds = signal<Set<string>>(new Set());

  /**
   * The group's calls segmented by backend batch.
   *
   * A caller that doesn't supply `batches` (older callers, tests) gets one
   * implicit unsummarized batch covering everything, which renders exactly
   * like the pre-batch rail.
   */
  batches = computed<ToolCallBatch[]>(() => {
    const group = this.group();
    if (group.batches?.length) return group.batches;
    return group.calls.length
      ? [{ key: group.calls[0].id, calls: group.calls }]
      : [];
  });

  /**
   * The one line shown collapsed.
   *
   * Order of preference: an explicit override; then the first batch that has
   * a model-generated summary; then the deterministic formatter.
   *
   * When summarized batches are followed by more rounds, the header says so
   * ("…, then 2 more steps") rather than presenting the opening round's line
   * as if it described the whole group — the expanded view carries the rest.
   * It still must not grow with the group, which is the entire point of
   * collapsing, so only ONE summary ever appears here.
   */
  headline = computed(() => {
    const override = this.group().groupSummary;
    if (override) return override;

    const batches = this.batches();
    const first = batches.findIndex((b) => !!b.summary);
    if (first === -1) return describeToolGroup(this.group().calls);

    const head = batches[first].summary!;
    const rest = batches.length - 1 - first;
    if (rest <= 0) return head;
    return `${head}, then ${rest} more step${rest === 1 ? '' : 's'}`;
  });

  /** True while any call in the group is still executing. */
  isRunning = computed(() =>
    this.group().calls.some(c => c.status === 'pending'),
  );

  /**
   * Total measured execution time across the group, or null when nothing has
   * been timed.
   *
   * Null rather than zero on a reloaded conversation: durations come from the
   * live `agent_status` stream and are deliberately not persisted, so showing
   * "0ms" for history would be a number the user could not trust.
   */
  totalDurationMs = computed(() => {
    const total = this.group().calls.reduce(
      (sum, c) => sum + (c.durationMs ?? 0),
      0,
    );
    return total > 0 ? total : null;
  });

  /** How many calls in the group failed, or null when none did. */
  failureCount = computed(() => {
    const failures = this.group().calls.filter(
      c => c.status === 'error' || c.result?.status === 'error',
    ).length;
    return failures > 0 ? failures : null;
  });

  /** The human line for one call inside the expanded rail. */
  describe(call: ToolCallDisplay): string {
    return call.summary || describeToolCall(call);
  }

  /** Toggle rail expand/collapse */
  toggleExpanded(): void {
    this.isExpanded.update(v => !v);
  }

  /** Toggle the raw input/result detail for a specific tool call. */
  toggleCallDetail(callId: string): void {
    this.expandedCallIds.update(ids => {
      const next = new Set(ids);
      if (next.has(callId)) {
        next.delete(callId);
      } else {
        next.add(callId);
      }
      return next;
    });
  }

  /**
   * Whether a call's raw detail is showing.
   *
   * A call still streaming long output (an artifact being generated) shows it
   * regardless of the toggle: that live preview is the whole point of the
   * streaming path, and folding it away would make a generating artifact look
   * like nothing was happening.
   */
  isCallDetailOpen(call: ToolCallDisplay): boolean {
    return this.isGenerating(call) || this.expandedCallIds().has(call.id);
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
      case 'complete':       return 'status-dot bg-state-success-500';
      case 'pending':        return 'status-dot bg-state-warning-400 shimmer';
      case 'error':          return 'status-dot bg-state-danger-500';
      case 'awaiting_auth':  return 'status-dot bg-primary-500 ring-2 ring-primary-300/40 dark:ring-primary-400/30';
      default:               return 'status-dot bg-gray-400';
    }
  }

  /**
   * True while a tool is still streaming its long output (e.g. an artifact)
   * and has not yet returned a result — drives the live "generating" preview.
   */
  isGenerating(call: ToolCallDisplay): boolean {
    return !!call.streamingContent && !call.result;
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
