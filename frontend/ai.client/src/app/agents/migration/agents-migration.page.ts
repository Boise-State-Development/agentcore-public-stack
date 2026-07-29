import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowRight,
  heroBookmark,
  heroChatBubbleLeftRight,
  heroCheck,
  heroCircleStack,
  heroCpuChip,
  heroDocumentText,
  heroLink,
  heroPuzzlePiece,
  heroShieldCheck,
  heroSparkles,
  heroSquares2x2,
  heroUserGroup,
  heroWrenchScrewdriver,
} from '@ng-icons/heroicons/outline';

/** One row of the "what an Agent adds" ledger. */
interface LedgerRow {
  label: string;
  detail: string;
  icon: string;
  /** Whether the old Assistant editor could do this at all. */
  assistant: 'yes' | 'no';
}

/** One capability card under "what this opens up". */
interface OpensUp {
  title: string;
  body: string;
  icon: string;
}

/**
 * The Assistants → Agents explainer, served at the old `/assistants` list URL.
 *
 * `/assistants` used to be a bare `redirectTo: 'agents'`. A silent redirect answers the
 * routing question and none of the human one: someone who bookmarked their Assistants list
 * lands on a page with a different name, a different tab strip and a different vocabulary,
 * with nothing to tell them their work came with it. The two *deep* links
 * (`/assistants/new`, `/assistants/:id/edit`) stay redirects on purpose — those are
 * intents, not browsing, and interrupting "edit this specific record" with an announcement
 * would be hostile. Only the list URL, which is the one people browse to, explains itself.
 *
 * The copy commits to three things, in this order, because that is the order the questions
 * actually arrive in: **where did my work go** (nowhere — the same agents, under a new
 * heading), **why** (because "agent" is the industry's word for this and "assistant" was
 * only ever ours), and **what it buys me** (the model/tools/skills/memory it can now hold,
 * the store, `@`-mention).
 *
 * The "why" is deliberately *not* argued as "we outgrew the old word". That framing asks the
 * reader to care about our vocabulary history, which is our problem and not theirs. Standard
 * naming is a benefit they can actually collect: it makes everything they read and learn
 * elsewhere transfer, in both directions. The capability list stays, but as evidence that
 * the standard word fits — not as the reason for the change.
 *
 * **Written for someone who has never used the platform**, not for someone who knows the
 * old surface: whoever clicks "Assistants" in the sidebar may be doing it to find out what
 * the word means. So the hero says what an agent *is* before it explains what changed, and
 * the copy avoids house vocabulary — no "records", no "knowledge base", no "system prompt",
 * no "bindings". If a term needs the platform explained first, it does not belong here.
 *
 * Everything claimed here is a surface that ships today. If a capability is removed or
 * gated, the claim on this page goes with it — an explainer that oversells is worse than
 * the redirect it replaced. (This is why scheduled runs are not mentioned; see `opensUp`.)
 */
@Component({
  selector: 'app-agents-migration',
  templateUrl: './agents-migration.page.html',
  styleUrl: './agents-migration.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, NgIcon],
  viewProviders: [
    provideIcons({
      heroArrowRight,
      heroBookmark,
      heroChatBubbleLeftRight,
      heroCheck,
      heroCircleStack,
      heroCpuChip,
      heroDocumentText,
      heroLink,
      heroPuzzlePiece,
      heroShieldCheck,
      heroSparkles,
      heroSquares2x2,
      heroUserGroup,
      heroWrenchScrewdriver,
    }),
  ],
})
export class AgentsMigrationPage {
  /**
   * What came across untouched. One line each — this section answers a worry, and a worry
   * is answered by a short concrete list, not by an explanation of the mechanism.
   */
  readonly carriedOver: readonly { label: string; detail: string; icon: string }[] = [
    {
      label: 'Every assistant you built',
      detail: 'The same name, the same instructions, the same opening questions.',
      icon: 'heroSparkles',
    },
    {
      label: 'Everything you gave it to read',
      detail: 'Your files and web pages are still there, and still used to answer.',
      icon: 'heroDocumentText',
    },
    {
      label: 'The people you shared with',
      detail: 'Anyone you shared with still has it, and can still do what you allowed.',
      icon: 'heroUserGroup',
    },
    {
      label: 'Your old links',
      detail: 'Bookmarks and links you sent people still work, and open the same thing.',
      icon: 'heroLink',
    },
  ];

  /**
   * The ledger. The point of the page in one table: everything the old editor did is a
   * "yes" in both columns, and the new rows are the reason the change was worth making.
   */
  readonly ledger: readonly LedgerRow[] = [
    {
      label: 'Instructions',
      detail: 'What you write to tell it its job, and how you want it to answer.',
      icon: 'heroDocumentText',
      assistant: 'yes',
    },
    {
      label: 'Things to read',
      detail: 'Files and web pages you hand it, so it answers from your material.',
      icon: 'heroCircleStack',
      assistant: 'yes',
    },
    {
      label: 'Sharing',
      detail: 'Keep it to yourself, share it with certain people, or open it to everyone.',
      icon: 'heroUserGroup',
      assistant: 'yes',
    },
    {
      label: 'A model you choose',
      detail: 'Pick which AI model answers for it, from the ones you have been given.',
      icon: 'heroCpuChip',
      assistant: 'no',
    },
    {
      label: 'Tools',
      detail: 'Let it do things, not just talk — look something up, work with a file, use a system you have connected.',
      icon: 'heroWrenchScrewdriver',
      assistant: 'no',
    },
    {
      label: 'Skills',
      detail: 'Ready-made know-how for a particular job, which it picks up only when that job comes up.',
      icon: 'heroPuzzlePiece',
      assistant: 'no',
    },
    {
      label: 'Memory',
      detail: 'Notes it keeps between conversations, so you are not starting from scratch each time.',
      icon: 'heroSparkles',
      assistant: 'no',
    },
  ];

  /**
   * Downstream surfaces the single noun unlocked. **Every card here must be a surface a
   * reader can go and use today** — an explainer that advertises something they cannot find
   * is worse than the silent redirect this page replaced.
   *
   * A "Run on a schedule" card sat here and has been pulled: scheduled runs work, but they
   * have no navigation of their own yet, so the card promised a feature with nowhere to go.
   * Put it back (with `heroClock`) when that surface is rolled out.
   */
  readonly opensUp: readonly OpensUp[] = [
    {
      title: 'Share it with everyone',
      body:
        'Put an agent up for review and it appears in Discover, where anyone here can find it and use it — instead of you sending a link to one person at a time.',
      icon: 'heroSquares2x2',
    },
    {
      title: 'Keep the ones you like',
      body:
        "Found an agent someone else built? Pin it, and it sits one click away in every chat. You may find a few already pinned for you.",
      icon: 'heroBookmark',
    },
    {
      title: 'Bring one into a chat',
      body:
        'Type @ in any conversation to hand what you are working on to a different agent, then carry on. Nothing to close, nothing to start over.',
      icon: 'heroChatBubbleLeftRight',
    },
  ];
}
