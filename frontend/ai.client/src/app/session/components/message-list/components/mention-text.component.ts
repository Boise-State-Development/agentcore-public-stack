import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { AgentMentionService } from '../../../../agents/services/agent-mention.service';

/** One run of message text, flagged as an Agent `@`-mention or as plain prose. */
export interface MentionSegment {
  text: string;
  isMention: boolean;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Split `text` into plain and `@Agent Name` runs.
 *
 * Matching is driven by the **known names** rather than by a `@\w+` pattern, because Agent
 * names contain spaces ("Brand Deck Builder") and because `@here`, an email address or a
 * npm scope in a code question must stay plain text. Longest name first, so an Agent whose
 * name prefixes another's cannot swallow the match.
 *
 * A mention must start a word and end at a word boundary — `foo@Agent` is an address, not a
 * mention.
 */
export function splitMentions(text: string, names: readonly string[]): MentionSegment[] {
  const candidates = names.filter((name) => name.trim().length > 0);
  if (candidates.length === 0 || !text.includes('@')) {
    return [{ text, isMention: false }];
  }

  const alternation = [...candidates]
    .sort((a, b) => b.length - a.length)
    .map(escapeRegExp)
    .join('|');
  const pattern = new RegExp(`(^|\\s)(@(?:${alternation}))(?![\\w-])`, 'gi');

  const segments: MentionSegment[] = [];
  let cursor = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    const mentionStart = match.index + match[1].length;
    if (mentionStart > cursor) {
      segments.push({ text: text.slice(cursor, mentionStart), isMention: false });
    }
    segments.push({ text: match[2], isMention: true });
    cursor = mentionStart + match[2].length;
  }

  if (cursor < text.length) {
    segments.push({ text: text.slice(cursor), isMention: false });
  }

  return segments.length > 0 ? segments : [{ text, isMention: false }];
}

/**
 * Renders user message text with `@`-mentions set apart from what the user typed.
 *
 * The literal `@Name` is what the composer left in the message (D11), so the thread reads
 * back exactly as it was sent; this only changes its weight so a mention is legible as an
 * address rather than as prose.
 *
 * Names come from {@link AgentMentionService}, the same list the composer's `@` menu offers.
 * It is session-cached and already warmed by the composer, so this costs nothing on the
 * render path; if it has not loaded yet the text simply renders plain and re-renders bold
 * when the signal fills in.
 */
@Component({
  selector: 'app-mention-text',
  changeDetection: ChangeDetectionStrategy.OnPush,
  // Every run is wrapped in a `<span>`, including the plain ones: a bare interpolation
  // sits in a text node whose surrounding indentation Angular collapses to a single
  // space, which would inject stray spaces into `whitespace-pre-wrap` message text.
  template: `@for (segment of segments(); track $index) {
    @if (segment.isMention) {
      <span class="font-semibold text-white">{{ segment.text }}</span>
    } @else {
      <span>{{ segment.text }}</span>
    }
  }`,
  styles: `
    :host {
      display: inline;
    }
  `,
})
export class MentionTextComponent {
  readonly text = input.required<string>();

  private readonly mentionService = inject(AgentMentionService);

  constructor() {
    // Warm the candidate list. Reloading straight into a thread renders its messages
    // before the composer has ever been focused, and without the names every mention
    // would read back as plain prose. `load()` is idempotent and session-cached.
    void this.mentionService.load();
  }

  readonly segments = computed(() =>
    splitMentions(
      this.text(),
      this.mentionService.mentionable().map((agent) => agent.name),
    ),
  );
}
