/**
 * The block a quiet-watcher run puts at the head of its user message
 * (`build_watch_prompt` in `apis/shared/scheduled_prompts/watch_run.py`): the
 * watcher's cursors, what it has already reported, and the run's instructions.
 * The model reads it; a person opening the run's conversation from the bell
 * should see only the schedule's own prompt.
 */
const WATCH_STATE_BLOCK = /^\s*<watch_state>[\s\S]*?<\/watch_state>\s*/;

/** `text` without a leading `<watch_state>` block, or `null` when it has none. */
export function withoutWatchState(text: string): string | null {
  return WATCH_STATE_BLOCK.test(text) ? text.replace(WATCH_STATE_BLOCK, '') : null;
}
