import {
  Component,
  input,
  signal,
  computed,
  ChangeDetectionStrategy,
} from '@angular/core';
import { KeyValuePipe } from '@angular/common';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationTriangle, heroLightBulb } from '@ng-icons/heroicons/outline';
import { JsonSyntaxHighlightPipe } from '../tool-use/json-syntax-highlight.pipe';
import {
  ToolCallGroup,
  ToolCallDisplay,
  ToolCallBatch,
  ToolRailStep,
  ThinkingStep,
} from './tool-rail.model';
import {
  formatThinkingDuration,
  latestCompleteSentence,
} from '../reasoning-content/reasoning-text';
import {
  describeToolCall,
  describeToolGroup,
  isAwaitingUserCall,
  isFailedCall,
} from './tool-summary';
import { PinScrollToBottomDirective } from './pin-scroll-to-bottom.directive';
import { ToolResultContent } from '../../../../services/models/message.model';

@Component({
  selector: 'app-tool-rail',
  templateUrl: './tool-rail.component.html',
  styleUrl: './tool-rail.component.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [JsonSyntaxHighlightPipe, KeyValuePipe, PinScrollToBottomDirective, NgIcon],
  providers: [provideIcons({ heroLightBulb, heroExclamationTriangle })],
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
   * Everything the rail did, in order: its batches, with any reasoning the
   * model did between them. A rail with no folded reasoning has one step per
   * batch, which renders exactly like the pre-thinking rail.
   */
  steps = computed<ToolRailStep[]>(() => {
    const steps = this.group().steps;
    if (steps?.length) return steps;
    return this.batches().map((batch) => ({ kind: 'batch', key: batch.key, batch }));
  });

  /** The reasoning spans folded into this rail. */
  thinkingSteps = computed<ThinkingStep[]>(() =>
    this.steps().flatMap((st) => (st.kind === 'thinking' ? [st.thinking] : [])),
  );

  /**
   * The span the model is reasoning in right now, if that is the rail's
   * newest step. Only the newest: an earlier span cannot still be open once
   * a tool call has followed it.
   */
  activeThinking = computed<ThinkingStep | null>(() => {
    const steps = this.steps();
    const last = steps[steps.length - 1];
    return last?.kind === 'thinking' && last.thinking.active ? last.thinking : null;
  });

  /**
   * The header's thinking readout: measured time when the stream timed it,
   * otherwise how many spans there were. A reloaded conversation has no
   * durations (they are live-only, like tool timings), and a guessed number
   * would be one the user could not trust. Null when nothing was folded in.
   */
  thinkingLabel = computed<string | null>(() => {
    const spans = this.thinkingSteps();
    if (!spans.length) return null;
    const timed = spans.filter((t) => typeof t.durationMs === 'number');
    if (timed.length) {
      const total = timed.reduce((sum, t) => sum + (t.durationMs ?? 0), 0);
      return `thought for ${formatThinkingDuration(total)}`;
    }
    // Still in the first span: the shimmering headline already says so.
    if (spans.every((t) => t.active)) return null;
    return spans.length === 1 ? '1 thought' : `${spans.length} thoughts`;
  });

  /**
   * The tool count beside the headline. Suppressed for a lone call in a rail
   * with no reasoning, where it would only restate the line; with reasoning
   * folded in the headline may be a thought, so the count is shown.
   */
  toolCountLabel = computed<string | null>(() => {
    const n = this.group().calls.length;
    if (n > 1) return `${n} tools`;
    return n === 1 && this.thinkingSteps().length ? '1 tool' : null;
  });

  /** The header button's accessible name. */
  toggleLabel = computed(() => {
    const what = this.thinkingSteps().length
      ? 'thinking and tool call details'
      : 'tool call details';
    return `${this.isExpanded() ? 'Hide' : 'Show'} ${what}`;
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

    // While the model reasons, say what it is on — the latest finished
    // sentence, the same line the standalone reasoning block shows — rather
    // than restating the last tool round it has already moved past.
    const thinking = this.activeThinking();
    if (thinking) return latestCompleteSentence(thinking.text) ?? 'Thinking';

    const batches = this.batches();
    const first = batches.findIndex((b) => !!b.summary);
    if (first === -1) return describeToolGroup(this.group().calls);

    const head = batches[first].summary!;
    const rest = batches.length - 1 - first;
    if (rest <= 0) return head;
    return `${head}, then ${rest} more step${rest === 1 ? '' : 's'}`;
  });

  /** True while any call is still executing or the model is reasoning. */
  isRunning = computed(
    () =>
      !!this.activeThinking() ||
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
    const failures = this.group().calls.filter(isFailedCall).length;
    return failures > 0 ? failures : null;
  });

  /** Whether a call failed (an App form waiting for the user has not). */
  callFailed(call: ToolCallDisplay): boolean {
    return isFailedCall(call);
  }

  /** The label over a call's raw result. */
  resultLabel(call: ToolCallDisplay): string {
    if (isAwaitingUserCall(call)) return 'Waiting for you';
    return isFailedCall(call) ? 'Error' : 'Result';
  }

  /** The human line for one call inside the expanded rail. */
  describe(call: ToolCallDisplay): string {
    return call.summary || describeToolCall(call);
  }

  /**
   * The one line for a reasoning span in the expanded list: its opening, or,
   * while it is still going, its latest finished sentence.
   */
  describeThinking(step: ThinkingStep): string {
    const text = step.text.trim();
    if (step.active) return latestCompleteSentence(step.text) ?? 'Thinking';
    if (text) return text.split(/\n+/)[0].trim();
    return step.redacted ? 'Reasoning redacted' : 'Thinking';
  }

  /** A reasoning span's measured length, or null on a reload. */
  thinkingDuration(step: ThinkingStep): string | null {
    return formatThinkingDuration(step.durationMs);
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

  /** Whether a reasoning span's full text is showing. */
  isThinkingOpen(step: ThinkingStep): boolean {
    return this.expandedCallIds().has(step.key);
  }

  /** CSS class for status dot */
  statusDotClass(call: ToolCallDisplay): string {
    switch (call.status) {
      case 'complete':       return 'status-dot bg-state-success-500';
      case 'pending':        return 'status-dot bg-state-warning-400 shimmer';
      case 'error':          return 'status-dot bg-state-danger-500';
      case 'awaiting_auth':
      case 'awaiting_user':  return 'status-dot bg-primary-500 ring-2 ring-primary-300/40 dark:ring-primary-400/30';
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

  /** Tool input params, one `key: value` per line */
  formatInput(inputObj: Record<string, unknown>): string {
    return Object.entries(inputObj)
      .map(([k, v]) => `${k}: ${JSON.stringify(v)}`)
      .join('\n');
  }

  /** Whether the result carries any text or JSON to show (images render separately). */
  hasResultBody(call: ToolCallDisplay): boolean {
    return !!call.result?.content?.some(item => item.text || item.json);
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
