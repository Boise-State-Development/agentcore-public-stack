import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { AgentMentionService } from '../../../../agents/services/agent-mention.service';
import { SkillCommandService } from '../../../../services/skill/skill-command.service';

/**
 * One run of message text, flagged as an Agent `@`-mention, a `/` skill command, or as
 * plain prose.
 */
export interface MentionSegment {
  text: string;
  isMention: boolean;
  isCommand?: boolean;
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
 * Refine the plain runs of `segments`, marking every `/slug` that names a known skill.
 *
 * Runs as a second pass over what {@link splitMentions} left as prose, rather than as one
 * combined pattern, because the two are matched on different things: a mention is matched
 * against known Agent *names* (which contain spaces), a command against known skill
 * *slugs* (which cannot). A mention is never re-examined — `@Brand/Deck` is one address.
 *
 * The token rule is the composer's, so what reads back as a command in the thread is
 * exactly what the composer would have sent as one: `and/or`, `24/7`, `src/app` and
 * `/usr/bin` stay prose, and an unknown slug stays prose too.
 */
export function splitSkillCommands(
  segments: MentionSegment[],
  slugs: readonly string[],
): MentionSegment[] {
  if (slugs.length === 0) {
    return segments;
  }

  const known = new Set(slugs.map((slug) => slug.toLowerCase()));
  const pattern = /(^|\s)(\/[a-z0-9][a-z0-9-]*)(?![\w\-/])/gi;

  return segments.flatMap((segment) => {
    if (segment.isMention || !segment.text.includes('/')) {
      return [segment];
    }

    const out: MentionSegment[] = [];
    let cursor = 0;
    let match: RegExpExecArray | null;
    pattern.lastIndex = 0;

    while ((match = pattern.exec(segment.text)) !== null) {
      if (!known.has(match[2].slice(1).toLowerCase())) {
        continue;
      }
      const start = match.index + match[1].length;
      if (start > cursor) {
        out.push({ text: segment.text.slice(cursor, start), isMention: false });
      }
      out.push({ text: match[2], isMention: false, isCommand: true });
      cursor = start + match[2].length;
    }

    if (out.length === 0) {
      return [segment];
    }
    if (cursor < segment.text.length) {
      out.push({ text: segment.text.slice(cursor), isMention: false });
    }
    return out;
  });
}

/**
 * Renders user message text with `@`-mentions set apart from what the user typed.
 *
 * The literal `@Name` is what the composer left in the message (D11), so the thread reads
 * back exactly as it was sent; this only changes its weight so a mention is legible as an
 * address rather than as prose.
 *
 * `/` skill commands get the same treatment for the same reason: the literal `/slug` is
 * both what the user typed and the binding itself, so a thread that rendered it as prose
 * would hide why one answer followed a recipe.
 *
 * Names come from {@link AgentMentionService} and slugs from {@link SkillCommandService},
 * the same lists the composer's two menus offer. Both are session-cached and already
 * warmed by the composer, so this costs nothing on the render path; if either has not
 * loaded yet the text simply renders plain and re-renders when the signal fills in.
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
    } @else if (segment.isCommand) {
      <span class="font-mono font-semibold text-white">{{ segment.text }}</span>
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
  private readonly skillCommandService = inject(SkillCommandService);

  constructor() {
    // Warm both candidate lists. Reloading straight into a thread renders its messages
    // before the composer has ever been focused, and without the names and slugs every
    // mention and command would read back as plain prose. Both `load()`s are idempotent
    // and session-cached.
    void this.mentionService.load();
    void this.skillCommandService.load();
  }

  readonly segments = computed(() =>
    splitSkillCommands(
      splitMentions(
        this.text(),
        this.mentionService.mentionable().map((agent) => agent.name),
      ),
      this.skillCommandService.commands().map((command) => command.slug),
    ),
  );
}
