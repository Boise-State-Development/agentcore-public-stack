import { Injectable, computed, inject } from '@angular/core';
import { SkillService } from './skill.service';

/** One row in the composer's `/` menu. */
export interface SkillCommand {
  skillId: string;
  /** The activation key. `/{slug}` is the literal command written into the message. */
  slug: string;
  name: string;
  description: string;
}

/** How many rows the menu shows at once. Matches the `@` menu. */
const MAX_RESULTS = 8;

/**
 * The `/`-command candidate list.
 *
 * **Scope is the skills the user has turned on**, which is the same set already
 * disclosed to the model in `<available_skills>` on every turn. That is what makes a
 * slash command cheap: invoking one changes nothing about the cacheable prefix, it only
 * appends a one-line directive to the turn's user message. Offering a skill that is
 * switched off would mean either silently widening the disclosure (a prefix rewrite, paid
 * at the cache-write premium on a keystroke) or showing a command that does nothing. The
 * menu's last row is "Browse skills →" instead, which is where turning one on belongs.
 *
 * The source is {@link SkillService}, loaded on demand and session-cached, so the menu
 * costs nothing on the keystroke path once anything has warmed it. A source that has not
 * loaded is simply absent — the composer must never fail because a list behind an optional
 * affordance did not arrive.
 */
@Injectable({ providedIn: 'root' })
export class SkillCommandService {
  private skillService = inject(SkillService);

  readonly loading = this.skillService.loading;

  /**
   * Enabled skills, ordered by name.
   *
   * Agent-locked conversations resolve through `visibleSkills`/`isSkillShownEnabled` like
   * every other skill surface, so a conversation bound to an Agent offers exactly that
   * Agent's bound skills as commands — the set the turn will actually disclose.
   */
  readonly commands = computed<SkillCommand[]>(() =>
    this.skillService
      .visibleSkills()
      .filter((skill) => this.skillService.isSkillShownEnabled(skill))
      // A skill with no slug is one served by a backend that predates the field. The
      // SPA and the backend deploy independently and in no enforced order, so drop it:
      // no command is right, a `/undefined` command is not.
      .filter((skill): skill is typeof skill & { slug: string } => !!skill.slug)
      .map<SkillCommand>((skill) => ({
        skillId: skill.skillId,
        slug: skill.slug,
        name: skill.displayName,
        description: skill.description,
      }))
      .sort((a, b) => a.name.localeCompare(b.name)),
  );

  /** Slug → command, for resolving what the user typed back to a skill. */
  private readonly bySlug = computed(
    () => new Map(this.commands().map((command) => [command.slug, command])),
  );

  /**
   * Warm the list. Safe to call on every composer focus — {@link SkillService} guards
   * against a concurrent load and caches for the session, and a 404 (the feature switched
   * off for this environment) resolves to an empty list rather than an error.
   */
  async load(): Promise<void> {
    if (this.skillService.initialized()) return;
    await this.skillService.loadSkills().catch(() => undefined);
  }

  /**
   * Rows matching what the user has typed after the `/`.
   *
   * The slug ranks above the display name, and a prefix above a substring: what a person
   * types after `/` is the command, and the command is the slug. Capped at
   * {@link MAX_RESULTS}, same as the `@` menu.
   */
  search(query: string): SkillCommand[] {
    const needle = query.trim().toLowerCase();
    const all = this.commands();
    if (!needle) return all.slice(0, MAX_RESULTS);

    return all
      .map((command) => ({ command, score: this.score(command, needle) }))
      .filter((entry) => entry.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, MAX_RESULTS)
      .map((entry) => entry.command);
  }

  /** The command for a slug, or undefined when the user has not turned that skill on. */
  bySlugOrUndefined(slug: string): SkillCommand | undefined {
    return this.bySlug().get(slug);
  }

  private score(command: SkillCommand, needle: string): number {
    const slug = command.slug.toLowerCase();
    if (slug.startsWith(needle)) return 4;
    if (slug.includes(needle)) return 3;
    const name = command.name.toLowerCase();
    if (name.startsWith(needle)) return 2;
    if (name.includes(needle)) return 1;
    return 0;
  }
}

/**
 * Every `/slug` in `text` that names a skill the user has turned on, de-duplicated and in
 * the order the slugs were given.
 *
 * The message text is the **only** source of truth for which skills a turn invokes — there
 * is no parallel selection signal to fall out of step with it. A slug is an unambiguous
 * single token (unlike an Agent name, which contains spaces and needs the `@` menu's
 * remembered pick), so reading it back out of the text is exact, and it means a command
 * typed by hand works identically to one picked from the menu.
 *
 * A command must start a word and must not be followed by another `/`, so `and/or`,
 * `https://x`, `src/app` and `/usr/bin/env` are all prose — the trailing-slash half is
 * what excludes an absolute path, which otherwise starts a word exactly like a command
 * does. `/unknown-thing` is prose too: only slugs the user can actually invoke resolve.
 */
export function findSkillCommands(text: string, slugs: readonly string[]): string[] {
  if (!text.includes('/') || slugs.length === 0) return [];

  const known = new Set(slugs);
  const found: string[] = [];
  const pattern = /(?:^|\s)\/([a-z0-9][a-z0-9-]*)(?![\w\-/])/gi;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    const slug = match[1].toLowerCase();
    if (known.has(slug) && !found.includes(slug)) {
      found.push(slug);
    }
  }

  return found;
}

/**
 * `text` with the first `/slug` command removed (and the space it left behind collapsed).
 *
 * This is what the chip's `✕` does: the command lives in the text, so un-invoking has to
 * edit the text — there is no separate binding to clear.
 */
export function removeSkillCommand(text: string, slug: string): string {
  // Slugs are `^[a-z0-9][a-z0-9-]*$` server-side, so nothing here needs escaping —
  // but building the pattern from an unescaped id is the kind of thing that stops
  // being true quietly, so escape anyway.
  const escaped = slug.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const pattern = new RegExp(`(^|\\s)/${escaped}(?![\\w\\-/])[ \\t]?`, 'gi');
  return text.replace(pattern, (_full, lead: string) => lead);
}
