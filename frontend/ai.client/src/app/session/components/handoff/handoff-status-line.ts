import { HandoffView, WaitingOn } from '../../services/handoff/handoff.models';

/** A handoff row's state as the user sees it: live while the target streams in this tab, persisted otherwise. */
export type HandoffRowState = 'running' | 'needs_input' | 'done' | 'stopped';

/** What the status line reads from the target's live stream in this tab (`ToolInsightService`). */
export interface LiveInsight {
  latestSummary(sessionId: string): string | null | undefined;
  status(sessionId: string): { phase: string; toolName?: string } | null | undefined;
}

/**
 * A handoff's status line (handoffs spec §4.3), shared by the handoff row and each step line of a
 * workflow run's row, so the two never word one state differently. While the target streams in
 * this tab it follows the live state (latest tool summary, then `agent_status`, then the request);
 * after that, the persisted `status` with `statusLine` / `waitingOn`.
 */
export function handoffStatusLine(
  row: HandoffView,
  state: HandoffRowState,
  insight: LiveInsight,
  providerName: (providerId: string) => string,
): string {
  const target = row.targetSessionId ?? '';
  switch (state) {
    case 'running': {
      const summary = insight.latestSummary(target);
      if (summary) return summary;
      const status = insight.status(target);
      if (status?.phase === 'tool_start' && status.toolName) return `Using ${status.toolName}`;
      if (status?.phase === 'thinking') return 'Thinking';
      return `Working on “${truncate(row.request, 60)}”`;
    }
    case 'needs_input':
      return waitingLine(row.waitingOn, providerName);
    case 'done': {
      const summary = insight.latestSummary(target) ?? row.statusLine;
      return summary ? `Finished · ${summary}` : 'Finished';
    }
    default:
      if (row.stoppedReason === 'connection_lost') return 'Stopped · connection lost';
      if (row.stoppedReason === 'not_started') return 'Didn’t start · open it to see why';
      return 'Stopped';
  }
}

export function waitingLine(waiting: WaitingOn | undefined, providerName: (providerId: string) => string): string {
  switch (waiting?.kind) {
    case 'oauth_consent':
      return waiting.name ? `Waiting for you to connect ${providerName(waiting.name)}` : 'Waiting for you to connect an account';
    case 'tool_approval':
      return waiting.name ? `Waiting for your approval to run ${waiting.name}` : 'Waiting for your approval';
    case 'user_question':
      return waiting.name || 'Waiting for your answer';
    case 'browser_login':
      return 'Waiting for you to sign in';
    default:
      return 'Needs your input';
  }
}

export function truncate(value: string, max: number): string {
  const text = value.trim();
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** Scroll an element to the middle of the thread and focus it without a second jump. */
export function bringIntoView(element: HTMLElement): void {
  if (typeof element.scrollIntoView === 'function') element.scrollIntoView({ block: 'center', behavior: 'smooth' });
  element.focus({ preventScroll: true });
}
