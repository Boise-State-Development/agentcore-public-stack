import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowRight,
  heroBookmark,
  heroChatBubbleLeftRight,
  heroCheck,
  heroCircleStack,
  heroClock,
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
 * actually arrive in: **where did my work go** (nowhere — same records, same ids), **why**
 * (one noun, and the old editor was strictly the smaller half of it), and **what it buys
 * me** (the bindings, the store, `@`-mention, scheduled runs).
 *
 * Everything claimed here is a surface that ships today. If a capability is removed or
 * gated, the claim on this page goes with it — an explainer that oversells is worse than
 * the redirect it replaced.
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
      heroClock,
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
      detail: 'Same name, same instructions, same conversation starters.',
      icon: 'heroSparkles',
    },
    {
      label: 'Your knowledge bases',
      detail: 'Documents, web sources and sync policies — still indexed, still answering.',
      icon: 'heroDocumentText',
    },
    {
      label: 'Who you shared with',
      detail: 'Viewers and editors kept exactly the access you gave them.',
      icon: 'heroUserGroup',
    },
    {
      label: 'Your old links',
      detail: 'Bookmarks and shared links still work, and open the same record.',
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
      detail: 'The system prompt that gives it a job and a manner.',
      icon: 'heroDocumentText',
      assistant: 'yes',
    },
    {
      label: 'Knowledge base',
      detail: 'Your documents and web sources, indexed and searchable.',
      icon: 'heroCircleStack',
      assistant: 'yes',
    },
    {
      label: 'Sharing',
      detail: 'Private, shared with named people, or open to the institution.',
      icon: 'heroUserGroup',
      assistant: 'yes',
    },
    {
      label: 'A model you choose',
      detail: 'Pick the model and tune its settings, within the bounds your role allows.',
      icon: 'heroCpuChip',
      assistant: 'no',
    },
    {
      label: 'Tools',
      detail: 'Give it the ability to act — search, run code, reach a connected system.',
      icon: 'heroWrenchScrewdriver',
      assistant: 'no',
    },
    {
      label: 'Skills',
      detail: 'Bundled know-how it loads on demand, so the instructions stay short.',
      icon: 'heroPuzzlePiece',
      assistant: 'no',
    },
    {
      label: 'Memory spaces',
      detail: 'A durable notebook it can read from, and write to when you allow it.',
      icon: 'heroSparkles',
      assistant: 'no',
    },
  ];

  /** Downstream surfaces the single noun unlocked. All of these ship today. */
  readonly opensUp: readonly OpensUp[] = [
    {
      title: 'A store to publish to',
      body:
        'Submit an agent for review and it appears in Discover, where anyone at the institution can find it — instead of you emailing a link to one person at a time.',
      icon: 'heroSquares2x2',
    },
    {
      title: 'Pin what you use',
      body:
        "Add someone else's agent to your own set and it is one click away from every chat. Your role may start you with a few already pinned.",
      icon: 'heroBookmark',
    },
    {
      title: 'Reach one mid-conversation',
      body:
        'Type @ in any chat to hand the current thread to a specialist agent, then carry on. No switching pages, no starting over.',
      icon: 'heroChatBubbleLeftRight',
    },
    {
      title: 'Run on a schedule',
      body:
        'Point a scheduled run at an agent and it works while you are not watching — a Monday digest, a nightly check, a weekly report.',
      icon: 'heroClock',
    },
  ];
}
