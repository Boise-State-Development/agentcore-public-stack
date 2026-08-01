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
  heroPaperAirplane,
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
 *
 * ## Two audiences, and why the second one gets two sentences
 *
 * "Assistants are now Agents" is only the *whole* truth for people who built on this
 * version of the site. There is a second group — everyone who built an assistant on the
 * **previous** boisestate.ai, now parked at `legacy.boisestate.ai` — for whom "nothing was
 * lost, it is all under Agents" is flatly wrong: that generation of assistant is not
 * compatible with this one, none of it came across, and re-creating it here is manual work.
 *
 * They are answered in the hero, in one paragraph: where their assistants are, and that
 * moving them is by hand. That is the entire answer, and it was deliberately cut back to
 * it — an earlier draft gave them a section with two comparison cards and a four-step
 * procedure, which turned a short piece of bad news into something that read like a
 * project. Someone who has to rebuild an assistant does not need it explained twice; they
 * need the link and the honest sentence.
 *
 * What the hero cannot do is carry the qualifier through the rest of the page, so every
 * unqualified "everything you built is here" below it is scoped to *this* version instead
 * ("here", "on this site"). A reassurance a reader cannot trust is worth less than no
 * reassurance: the person who follows "nothing to do" and finds an empty list stops
 * believing the rest of the page, including the parts that are true for them.
 *
 * The one place precision beats brevity is the link — it *reads* as `legacy.boisestate.ai`
 * and *points* at `?segment=my`, so the sentence stays plain while the click still lands on
 * their own list rather than a home page they have to search from.
 *
 * ⚠️ This whole page is host-gated (production apex + localhost; see
 * `shared/utils/legacy-migration-host.ts`) and comes out with the legacy site.
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
      heroPaperAirplane,
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
   * Deep link to the reader's **own** assistants on the previous site, not its home page.
   * `?segment=my` is the legacy list's "mine" tab. Someone sent to a landing page has to
   * re-find their work before they can start copying it, and that re-finding is where
   * people give up.
   */
  readonly legacyAssistantsUrl = 'https://legacy.boisestate.ai/assistants?segment=my';

  /**
   * What came across untouched. One line each — this section answers a worry, and a worry
   * is answered by a short concrete list, not by an explanation of the mechanism.
   *
   * ⚠️ Scoped to assistants built **in this version**. See the class doc: unqualified here
   * is a lie to the legacy half of the audience.
   */
  readonly carriedOver: readonly { label: string; detail: string; icon: string }[] = [
    {
      label: 'Every assistant you built here',
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
   *
   * A "Share it with everyone" card was also pulled — not because it was untrue, but because
   * the marketplace outgrew a card. It has its own section below, since "how do I get one
   * published" is a procedure and a card can only tease it.
   */
  readonly opensUp: readonly OpensUp[] = [
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

  /**
   * How an agent gets published. Three steps because there are three, and each one names
   * who acts — you, an admin, everyone — since the question underneath "how do I publish" is
   * really "who decides, and when does it stop being mine to change".
   *
   * The decider is named as **an admin**, not "someone" or "a person". Anonymous review
   * ("someone reads it") makes an accountable process sound like a committee behind a
   * curtain; "an admin" is a role the reader already knows exists here, so the same sentence
   * reads as governance rather than as a mystery.
   *
   * No "shelf" either, in any of the three. It is the marketplace metaphor talking, and it
   * only makes sense to someone already picturing a store — the reader who needs this list
   * most is the one who is not. The literal words the UI uses (a **category** you choose,
   * **Discover** where it lands) cost nothing and need no decoding. The author-facing
   * surfaces this page hands off to were brought in step: the submit dialog's Category and
   * Tagline hints, its "Note to the admin", and the withdrawal confirmation in
   * `share-agent-dialog`. **Shelf survives in code** — `CategoryShelf`, the `shelves` signal,
   * the internal comments — because that is the domain model's name, not copy a user reads.
   * ⚠️ Still on the admin side: `categories.page.ts`, `publishers.page.ts` and the
   * reachability warning in `models/reachability.ts` say it to reviewers.
   *
   * ⚠️ Every claim here is asserted against the shipped flow, not the intent:
   * `submit-listing-dialog.component.ts` for what the author fills in (category, tagline,
   * optional note, and the public checkbox), `LISTING_STATE_LABELS` in `store.model.ts` for
   * the vocabulary an admin's decision comes back in, and `PUBLISHED_VERSION_TOOLTIP` in
   * the admin marketplace model for the snapshot rule in step 3. If the review flow changes,
   * this list changes with it — the fastest way to make a governed process feel arbitrary is
   * to describe it inaccurately.
   */
  readonly marketplaceSteps: readonly { title: string; body: string; icon: string }[] = [
    {
      title: 'You submit it',
      body:
        'Choose the category it belongs in, write the one line that sits under its name, and add a note to the admin if there is something they should know. Agents start private, so you confirm you are making this one public.',
      icon: 'heroPaperAirplane',
    },
    {
      title: 'An admin reviews it',
      body:
        'An admin reads it before anyone else can. They publish it, or come back asking for changes — either way you see the decision, and their reasoning, on your own agent.',
      icon: 'heroShieldCheck',
    },
    {
      title: 'It goes live in Discover',
      body:
        'Published, it appears in Discover for everyone here to find, use and pin. What they run is the approved version, so you can keep working on yours — your next version goes live when it is approved too.',
      icon: 'heroSquares2x2',
    },
  ];
}
