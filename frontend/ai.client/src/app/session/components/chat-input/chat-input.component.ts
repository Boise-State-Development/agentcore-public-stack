import {
  Component,
  signal,
  output,
  inject,
  input,
  computed,
  viewChild,
  effect,
  untracked,
  afterNextRender,
  ElementRef,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { v4 as uuidv4 } from 'uuid';
import { Router } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroPlus,
  heroArrowTurnDownRight,
  heroClock,
  heroMicrophone,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import { heroArrowUpSolid, heroStopSolid } from '@ng-icons/heroicons/solid';
import { ModelDropdownComponent } from '../../../components/model-dropdown/model-dropdown.component';
import { AnnouncementBannerComponent } from '../../../components/announcement-banner/announcement-banner.component';
import { QuotaWarningBannerComponent } from '../../../components/quota-warning-banner/quota-warning-banner.component';
import { TooltipDirective } from '../../../components/tooltip';
import { FileCardComponent } from '../../../components/file-card';
import { StorageQuotaBannerComponent } from '../../../components/storage-quota-banner';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';
import {
  FileUploadService,
  PendingUpload,
  ALLOWED_EXTENSIONS,
  maxFileSizeFor,
  MAX_FILES_PER_MESSAGE,
  formatBytes
} from '../../../services/file-upload';
import { ToastService } from '../../../services/toast/toast.service';
import { ToolService } from '../../../services/tool/tool.service';
import { VoiceChatService, type VoiceStatus } from '../../services/voice';
import { SystemPromptsService } from '../../../services/system-prompts/system-prompts.service';
import {
  AgentMentionService,
  MentionableAgent,
} from '../../../agents/services/agent-mention.service';
import { AgentMentionMenuComponent } from './agent-mention-menu.component';
import {
  SkillCommand,
  SkillCommandService,
  findSkillCommands,
  removeSkillCommand,
} from '../../../services/skill/skill-command.service';
import { SkillCommandMenuComponent } from './skill-command-menu.component';
import { SteeringService } from '../../services/chat/steering.service';
import { ComposerDraftService } from '../../services/session/composer-draft.service';

// Must stay in sync with the inline min-height/max-height on the textarea in
// chat-input.component.html.
const MIN_TEXTAREA_HEIGHT_PX = 60;
const MAX_TEXTAREA_HEIGHT_PX = 200;

/** The composer's resting placeholder, and the string the rotation settles back on. */
const IDLE_PLACEHOLDER = 'How can I help you today?';

/** Dwell per rotating hint. Long enough to read a short line without hurrying. */
const HINT_ROTATION_MS = 4500;

/**
 * How many times the hints cycle before the composer comes to rest.
 *
 * One pass was not enough to be worth building: at three hints it was over
 * thirteen seconds after mount, most of which is page load and the user
 * reading the greeting above the composer — so the thing they were meant to
 * discover had finished before they looked down. Three passes is about forty
 * seconds of an empty composer, and the first keystroke ends it early.
 */
const HINT_PASSES = 3;

interface Message {
  content: string;
  timestamp: Date;
  fileUploadIds?: string[];
  /**
   * Marketplace D11: the Agent `@`-mentioned for **this turn only**. The conversation is
   * not bound to it — the next message with no mention is plain chat again.
   */
  mentionAgentId?: string;
  /**
   * Skills the user invoked with a `/` slash command in this message. Derived from the
   * message text, so it is always exactly what the thread will show.
   */
  invokedSkillIds?: string[];
}

/**
 * A follow-up the user sent while a response was still streaming. Carries
 * everything a real send carries except the timestamp, which is stamped at
 * flush time: the message enters the conversation when it is actually sent, and
 * stamping it at queue time would sort it ahead of the assistant reply that was
 * still streaming when the user typed it.
 */
interface QueuedMessage {
  /**
   * Client-minted id, and the whole reason mid-turn steering can be
   * idempotent: it is the id armed on the backend, the id the runtime clears
   * once the injection is committed, and the id `steering_applied` names back.
   */
  id: string;
  content: string;
  fileUploadIds?: string[];
  mentionAgentId?: string;
  invokedSkillIds?: string[];
  /**
   * True once the backend has confirmed this entry is armed against the
   * running turn, so it may land at the agent's next tool boundary rather than
   * waiting for the turn to end.
   *
   * Armed is not delivered. If the ack never arrives — the turn ended first,
   * the stream dropped — the entry flushes on the falling edge exactly as an
   * unarmed one does. That can double-send text the agent already read if an
   * ack was lost in flight, which is the deliberate trade: a visible duplicate
   * the user can see and work around, over silently swallowing something they
   * said.
   */
  armed?: boolean;
}

/** The `@…` or `/…` the caret is currently sitting in, and where it starts in the text. */
interface MentionToken {
  query: string;
  start: number;
}

@Component({
  selector: 'app-chat-input',
  imports: [AnnouncementBannerComponent, FormsModule, ModelDropdownComponent, NgIcon, QuotaWarningBannerComponent, StorageQuotaBannerComponent, TooltipDirective, FileCardComponent, AgentMentionMenuComponent, SkillCommandMenuComponent, SpinnerComponent],
  // `relative` is the anchor the announcement banner floats against — it sits
  // `bottom-full` of this host, above the quota tabs and clear of the composer.
  host: { class: 'relative block' },
  providers: [
    provideIcons({
      heroPlus,
      heroArrowTurnDownRight,
      heroClock,
      heroMicrophone,
      heroXMark,
      heroStopSolid,
      heroArrowUpSolid
    })
  ],
  templateUrl: './chat-input.component.html',
  styleUrl: './chat-input.component.css'
})
export class ChatInputComponent {
  // Service injection
  private readonly fileUploadService = inject(FileUploadService);
  private readonly toastService = inject(ToastService);
  private readonly steering = inject(SteeringService);
  private readonly composerDraft = inject(ComposerDraftService);
  private readonly toolService = inject(ToolService);
  private readonly voiceChatService = inject(VoiceChatService);
  protected readonly systemPromptsService = inject(SystemPromptsService);
  private readonly router = inject(Router);

  // Input: session ID for file uploads
  readonly sessionId = input<string | null>(null);

  // Input: loading state (required - parent must provide this)
  readonly isChatLoading = input<boolean>(false);

  // Input: show file attachment controls (defaults to true)
  readonly showFileControls = input<boolean>(true);

  // Input: show voice mode toggle (defaults to true). Disabled where voice
  // is not meaningful, e.g. the assistant editor preview.
  readonly showVoiceControl = input<boolean>(true);

  // Input: auto-focus the textarea on load and session change (defaults to true).
  // Disabled where the input sits beside an editable form (e.g. assistant preview).
  readonly autoFocus = input<boolean>(true);

  // Input: offer the `@`-mention menu (defaults to true). Off where handing the turn to
  // another Agent makes no sense — the Agent editor's own preview, which is already
  // running the Agent being edited.
  readonly showAgentMentions = input<boolean>(true);

  // Input: offer the `/` skill-command menu (defaults to true). Off in the embedded
  // previews for the same reason as `@`-mentions — those panes exercise one Agent whose
  // skills the Agent itself dictates, so a menu built from the *user's* enabled skills
  // would offer commands the previewed turn does not disclose.
  readonly showSkillCommands = input<boolean>(true);

  /**
   * Whether an announcement banner may float above this composer.
   *
   * True for the real chat and false for the embedded preview panes — an
   * agent-preview or a marketplace test-drive is exercising one specific
   * agent, and a platform-wide "new models are available" notice appearing
   * inside that small pane reads as a bug rather than an announcement.
   * Follows the same opt-out shape as the `show*` controls above.
   */
  readonly showAnnouncements = input<boolean>(true);

  /**
   * Which side of this composer an announcement takes. Supplied by the
   * container, which knows whether the composer is centred (empty state) or
   * pinned to the bottom (a conversation).
   */
  readonly announcementPlacement = input<'above' | 'below'>('above');

  private readonly messageInput = viewChild<ElementRef<HTMLTextAreaElement>>('messageInput');

  // Use the input directly - parent controls loading state
  protected readonly isLoading = computed(() => this.isChatLoading());

  // Signals for state management
  userInput = signal('');
  isFocused = signal(false);
  isDraggingOver = signal(false);

  /**
   * Follow-ups typed while a response was streaming, oldest first.
   *
   * Enter used to mean Stop mid-stream, so a follow-up typed out of habit
   * killed the response the user was waiting on — and a send that raced the
   * single-flight guard cleared the composer before the 409 came back, eating
   * the text outright. Queueing removes both: the draft is never destroyed, so
   * there is nothing to recover, and the guard becomes unreachable for
   * user-typed follow-ups rather than merely survivable.
   *
   * A list rather than one slot, because a queue that silently drops the second
   * entry is the same bug in a smaller box.
   */
  readonly queuedMessages = signal<QueuedMessage[]>([]);

  // Track drag enter/leave depth to handle nested elements
  private dragCounter = 0;

  /** Whether a turn has been observed in flight since the last queue flush. */
  private turnInFlight = false;

  // Output events
  fileAttached = output<File>();
  messageSubmitted = output<Message>();
  messageCancelled = output<void>();

  // File upload state from service
  readonly pendingUploads = this.fileUploadService.pendingUploadsList;
  readonly hasActivePendingUploads = this.fileUploadService.hasActivePendingUploads;
  readonly readyUploadIds = this.fileUploadService.readyUploadIds;

  // Computed: show file attachments area
  readonly showFileAttachments = computed(() => this.pendingUploads().length > 0);

  /**
   * Whether a follow-up typed right now could land *inside* the running turn.
   *
   * True once the turn has called at least one tool, because a tool boundary is
   * the only place an injection can go — a pure-text turn has none, which is
   * why PR #916's end-of-turn flush is a permanent fallback rather than a
   * transitional one. See docs/specs/mid-turn-steering.md (D5).
   */
  protected readonly canSteer = computed(
    () => this.isLoading() && this.steering.canSteer(this.sessionId()),
  );

  /**
   * The composer stays usable mid-stream, so the placeholder is the only place
   * the queueing behaviour announces itself before the user tries it — and the
   * two behaviours make different promises, so it has to say which one is on
   * offer. Overstating this is the failure that matters: a user told their
   * follow-up lands at the next step, who then watches it sit until the turn
   * ends, learns not to trust the affordance.
   */
  protected readonly placeholder = computed(() => {
    // A prompt awaiting an answer holds the queue, and the turn is NOT
    // streaming while it does — so the idle placeholder would be the most
    // wrong of the three: it promises immediate delivery on the one path that
    // waits the longest.
    if (this.queueHeld()) {
      return 'Send a follow-up — it goes in when you answer above';
    }
    if (!this.isLoading()) return IDLE_PLACEHOLDER;
    return this.canSteer()
      ? 'Send a follow-up — it goes in at the next step'
      : 'Send a follow-up — it goes out when this response finishes';
  });

  /**
   * Whether this conversation's queue is waiting on a consent / approval
   * prompt rather than on a running turn. Read by the placeholder and the
   * queued chips so a held follow-up explains itself instead of looking stuck.
   */
  protected readonly queueHeld = computed(() =>
    this.steering.shouldHoldQueue(this.sessionId()),
  );

  // =========================================================================
  // Rotating discovery hints
  //
  // `@` and `/` are the two shortcuts nothing on the page advertises: each one
  // only reveals itself once you have already typed the character that opens
  // its menu. The empty composer is where a user looks when they do not yet
  // know what to type, so it is where the hint belongs.
  //
  // Three rules keep this from being the kind of animation people file bugs
  // about:
  //
  // 1. **It is decoration, not information.** The native `placeholder`
  //    attribute never rotates — assistive tech reads one stable string. The
  //    visible line is an `aria-hidden` overlay painted over a placeholder
  //    that is transparent but still there. A placeholder that re-announced
  //    itself every few seconds would be a screen-reader defect, not a
  //    feature.
  // 2. **It stops.** One pass through the list, then it settles on the idle
  //    string for good, and the first keystroke settles it on the spot. Since
  //    nothing auto-updates indefinitely, WCAG 2.2.2 asks for no pause control
  //    that we would then have to fit into the composer's chrome.
  // 3. **It honours `prefers-reduced-motion`.** Reduce means no rotation at
  //    all — a plain static placeholder — not the same rotation with the fade
  //    taken off.
  //
  // Hints are offered only for surfaces this composer actually has: an
  // environment with Agents switched off, or a user with no skills enabled, is
  // never told to type a character that opens an empty menu.
  // =========================================================================

  /** How many times the hint has advanced since the composer last came alive. */
  private readonly hintStep = signal(0);

  /** Set once the rotation is over — by finishing its passes, or by the user typing. */
  private readonly hintsSettled = signal(false);

  /**
   * Read once at construction. A preference change mid-session lands on the
   * next load, which is acceptable for something that stops after one pass;
   * `matchMedia` is guarded because the specs run in jsdom, which has none.
   */
  private readonly prefersReducedMotion =
    typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
      : false;

  protected readonly composerHints = computed<string[]>(() => {
    const hints = [IDLE_PLACEHOLDER];
    if (this.showAgentMentions() && this.mentionService.mentionable().length > 0) {
      hints.push('Type @ to hand this turn to one of your agents');
    }
    if (this.showSkillCommands() && this.skillCommandService.commands().length > 0) {
      hints.push('Type / to run one of your skills');
    }
    return hints;
  });

  /**
   * Whether the overlay is painting — and so also the gate on the textarea's
   * placeholder colour, because the two must never both be visible.
   *
   * Deliberately **not** gated on `hintsSettled`. The overlay stays up when the
   * rotation ends, resting on the idle line, so coming to rest is a cross-fade
   * onto a string rather than an unmount. Unmounting would exit-animate a copy
   * of the idle line straight off the native placeholder underneath, which
   * spells the same words — a ghost double-image on the one transition every
   * user sees.
   */
  protected readonly showHintOverlay = computed(
    () =>
      !this.prefersReducedMotion &&
      this.userInput().length === 0 &&
      !this.isLoading() &&
      !this.queueHeld() &&
      this.composerHints().length > 1,
  );

  /** Whether the hint is still advancing, as opposed to resting on the idle line. */
  protected readonly rotateHints = computed(
    () => this.showHintOverlay() && !this.hintsSettled(),
  );

  /**
   * The hint to paint, as a single-item list.
   *
   * A list rather than a string because `@for`'s `track` is what swaps the
   * node on each rotation, and the node is what carries `animate.enter` /
   * `animate.leave`: a reused element with a new interpolation animates
   * nothing. Both nodes are absolutely positioned in the same spot, so the
   * outgoing line rises out while the incoming one rises in.
   */
  protected readonly visibleHint = computed<string[]>(() => {
    if (!this.showHintOverlay()) return [];
    const hints = this.composerHints();
    return [hints[this.hintStep() % hints.length]];
  });

  /**
   * Come to rest on the idle line. Called when the passes run out, and on the
   * first keystroke — a user who is typing has stopped needing to be told how
   * to start.
   */
  private settleHints(): void {
    this.hintsSettled.set(true);
    this.hintStep.set(0);
  }

  // Computed: can submit (has content or ready files)
  readonly canSubmit = computed(() => {
    const hasText = this.userInput().trim().length > 0;
    const hasReadyFiles = this.readyUploadIds().length > 0;
    const isUploading = this.hasActivePendingUploads();
    return (hasText || hasReadyFiles) && !isUploading;
  });

  // Allowed file types for input accept attribute
  readonly acceptedFileTypes = ALLOWED_EXTENSIONS.join(',');

  // Voice state (from VoiceChatService)
  readonly voiceStatus = this.voiceChatService.status;
  readonly isVoiceActive = this.voiceChatService.isVoiceActive;
  readonly voiceTranscript = this.voiceChatService.agentTranscript;

  readonly voiceButtonClass = computed(() => {
    const status = this.voiceStatus();
    const base = 'flex size-10 items-center justify-center rounded-lg transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-primary)]';
    switch (status) {
      case 'listening':
        return `${base} bg-state-danger-100 text-state-danger-700 dark:bg-state-danger-900/30 dark:text-state-danger-400 animate-pulse`;
      case 'speaking':
        return `${base} bg-state-success-100 text-state-success-700 dark:bg-state-success-900/30 dark:text-state-success-400`;
      case 'connecting':
        return `${base} bg-state-warning-100 text-state-warning-700 dark:bg-state-warning-900/30 dark:text-state-warning-400`;
      default:
        return `${base} text-gray-500 dark:text-gray-400 hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-white/5 dark:hover:text-gray-300`;
    }
  });

  readonly voiceAriaLabel = computed(() => {
    const status = this.voiceStatus();
    switch (status) {
      case 'listening': return 'Listening... Click to stop voice';
      case 'speaking': return 'Agent is speaking... Click to stop voice';
      case 'connecting': return 'Connecting voice...';
      default: return 'Start voice conversation';
    }
  });

  readonly voiceTooltip = computed(() => {
    const status = this.voiceStatus();
    switch (status) {
      case 'listening': return 'Listening...';
      case 'speaking': return 'Speaking...';
      case 'connecting': return 'Connecting...';
      default: return 'Voice mode';
    }
  });

  // =========================================================================
  // `@`-mention (Marketplace D11)
  //
  // Mentioning an Agent hands **that turn** to its model, tools and skills without
  // leaving the thread. The conversation is not bound to it: the mention rides one
  // request, and the next plain message is plain chat again.
  //
  // The menu opens on an `@` that starts a word and closes on anything that ends the
  // token — whitespace, a second `@`, moving the caret away, or Escape. `mentionedAgent`
  // survives the menu closing, because the *selection* is a property of the pending turn
  // while the menu is a property of what is being typed right now.
  // =========================================================================
  private readonly mentionService = inject(AgentMentionService);

  /** The Agent this turn will be handed to, once picked. Cleared on submit. */
  readonly mentionedAgent = signal<MentionableAgent | null>(null);

  /** The `@…` token under the caret, or null when the caret is not in one. */
  private readonly mentionToken = signal<MentionToken | null>(null);

  readonly mentionActiveIndex = signal(0);

  readonly mentionResults = computed(() => {
    const token = this.mentionToken();
    return token ? this.mentionService.search(token.query) : [];
  });

  /**
   * The menu opens only when there is something to offer, and only when the turn is not
   * already spoken for.
   *
   * A user with no Agents — or an environment with the whole surface switched off, where
   * both source calls 404 — must be able to type `@` in a sentence without a menu
   * appearing to say it has nothing. Once the lists load, the token is still set and the
   * menu pops in on its own.
   *
   * **One mention per turn.** D11 hands *the* turn to *an* Agent, so a second mention has
   * nothing to mean. Suppressing the menu while one is pending also stops it re-opening
   * when the caret lands back inside the `@Name` text already committed — names contain
   * spaces, so that would otherwise happen constantly. The chip's `✕` is how you change
   * your mind.
   */
  readonly isMentionMenuOpen = computed(
    () =>
      this.showAgentMentions() &&
      this.mentionToken() !== null &&
      this.mentionedAgent() === null &&
      this.mentionService.mentionable().length > 0,
  );

  readonly mentionQuery = computed(() => this.mentionToken()?.query ?? '');

  // =========================================================================
  // `/` skill commands
  //
  // Typing `/pdf-workflows` invokes that skill for **this message**. The scope is
  // deliberately the skills the user already has switched on, so the command changes
  // nothing about what the turn discloses to the model — the same `<available_skills>`
  // block ships either way and the cacheable prefix is untouched. All the backend adds is
  // a one-line directive telling the model to activate the named skill before answering.
  //
  // **The text is the state.** Unlike the `@` menu, there is no remembered pick: the
  // invoked set is derived from what is in the composer, because a slug is a single
  // unambiguous token and reading it back is exact. That makes a hand-typed command work
  // identically to a menu pick, and it removes the whole class of bugs where a chip and
  // the message text disagree about what is about to happen.
  // =========================================================================
  private readonly skillCommandService = inject(SkillCommandService);

  /** The `/…` token under the caret, or null when the caret is not in one. */
  private readonly skillToken = signal<MentionToken | null>(null);

  readonly skillActiveIndex = signal(0);

  readonly skillResults = computed(() => {
    const token = this.skillToken();
    return token ? this.skillCommandService.search(token.query) : [];
  });

  /**
   * The menu opens only when there is something to offer.
   *
   * `/` is far more common in prose than `@` — dates, fractions, "and/or", paths, URLs —
   * so the token rule below is what does the real work; this only stops an empty menu
   * appearing for a user who has no skills switched on.
   */
  readonly isSkillMenuOpen = computed(
    () =>
      this.showSkillCommands() &&
      this.skillToken() !== null &&
      this.skillCommandService.commands().length > 0,
  );

  readonly skillQuery = computed(() => this.skillToken()?.query ?? '');

  /** The skills this message will invoke, read straight out of the composer text. */
  readonly invokedSkills = computed<SkillCommand[]>(() => {
    if (!this.showSkillCommands()) return [];
    const commands = this.skillCommandService.commands();
    return findSkillCommands(
      this.userInput(),
      commands.map((command) => command.slug),
    )
      .map((slug) => commands.find((command) => command.slug === slug))
      .filter((command): command is SkillCommand => command !== undefined);
  });

  private invokedSkillIds(): string[] | undefined {
    const ids = this.invokedSkills().map((command) => command.skillId);
    return ids.length > 0 ? ids : undefined;
  }

  /**
   * Only one of the two menus is ever open. `@` wins a tie because it is the narrower
   * token (a `@` cannot also be the start of a `/` command), and because both menus
   * claiming the arrow keys would make neither usable.
   */
  readonly isSkillMenuVisible = computed(() => this.isSkillMenuOpen() && !this.isMentionMenuOpen());

  constructor() {
    // Walk the rotating hints once, then stop for good. The interval is torn
    // down the moment `rotateHints` goes false — the user typed, a turn
    // started, or the pass finished — so nothing ticks behind an idle tab's
    // composer for the life of the session.
    effect((onCleanup) => {
      if (!this.rotateHints()) return;
      // A whole number of passes, so the last advance lands back on the idle
      // line — the rotation always comes to rest on the string the composer
      // would have shown anyway.
      const steps = this.composerHints().length * HINT_PASSES;
      const timer = setInterval(() => {
        const step = untracked(this.hintStep) + 1;
        if (step > steps) {
          this.settleHints();
          return;
        }
        this.hintStep.set(step);
      }, HINT_ROTATION_MS);
      onCleanup(() => clearInterval(timer));
    });

    // Focus the textarea on first mount...
    afterNextRender(() => this.focusInput());
    // ...and whenever the session changes (new or existing). When switching
    // between sessions in the messages view the component instance is reused,
    // so afterNextRender alone would not refocus.
    effect(() => {
      const sessionId = this.sessionId();
      this.focusInput();
      // A brand-new conversation is the one moment the hints are worth showing
      // again: the composer is empty, the user has not committed to anything,
      // and this instance is reused across sessions so nothing else would
      // reset them. Opening an *existing* conversation deliberately does not
      // restart them — that would turn a hint into a tic.
      if (sessionId === null) {
        this.hintStep.set(0);
        this.hintsSettled.set(false);
      }
    });

    // A feature (today: the feedback retry-with-correction) can hand this
    // composer a draft for its session. Set it, size the textarea to it and
    // focus so the user edits and sends; never submit on their behalf.
    effect(() => {
      const draft = this.composerDraft.pending();
      const sessionId = untracked(this.sessionId);
      if (!draft || draft.sessionId !== sessionId) return;
      const taken = untracked(() => this.composerDraft.consume(sessionId));
      if (!taken) return;
      this.userInput.set(taken.text);
      const textarea = this.messageInput()?.nativeElement;
      if (textarea) {
        textarea.value = taken.text;
        this.autoResize(textarea);
        textarea.focus();
        textarea.setSelectionRange(taken.text.length, taken.text.length);
      }
    });

    // Mirror the queue into SteeringService so the resume path can carry it
    // into the turn it restarts without reaching into this component.
    effect(() => {
      const sessionId = this.sessionId();
      const queue = this.queuedMessages();
      untracked(() =>
        this.steering.publishQueue(
          sessionId,
          queue.map(q => ({ id: q.id, text: q.content })),
        ),
      );
    });

    // Drop entries the backend confirmed it injected mid-turn.
    //
    // This is what keeps the two delivery paths from both firing: once
    // `steering_applied` lands, the text is in conversation history and the
    // falling-edge flush below must not send it again. It runs on the ack, not
    // on the falling edge, precisely so it wins that race — the ack is emitted
    // ahead of `done`, and `isChatLoading` only falls after the stream closes.
    effect(() => {
      const applied = this.steering.applied();
      if (applied.length === 0) return;
      untracked(() => {
        const queue = this.queuedMessages();
        for (const entryId of applied) {
          // Consume regardless of whether we hold the entry: an ack for
          // another composer's entry (or one already removed) is finished
          // business either way, and leaving it would grow the list forever.
          this.steering.consumeApplied(entryId);
        }
        const appliedIds = new Set(applied);
        const remaining = queue.filter(q => !appliedIds.has(q.id));
        if (remaining.length !== queue.length) {
          this.queuedMessages.set(remaining);
        }
      });
    });

    // Send one queued follow-up per completed turn.
    //
    // Edge-triggered on loading going true -> false, not level-triggered on
    // "idle with something waiting". The parent only raises `isChatLoading`
    // after `messageSubmitted` round-trips through the chat service, so a level
    // check sees itself as still-idle immediately after emitting and drains the
    // whole queue in one pass — straight into the single-flight guard that
    // rejects the second turn. Consuming the edge is what holds it to one.
    //
    // The parent cannot tell us *how* the turn ended, and it doesn't need to:
    // done, aborted and errored all land on the same falling edge, which is
    // what makes the three paths symmetric without three code paths.
    effect(() => {
      if (this.isLoading()) {
        this.turnInFlight = true;
        return;
      }
      if (!this.turnInFlight) return;
      // Hold while a consent / approval prompt for this conversation is
      // waiting on the user. Flushing here would start a new turn that
      // abandons the paused one they are in the middle of answering, and can
      // race the resume that follows into the single-flight guard. Answering
      // the prompt carries the queue into the resumed turn; dismissing it
      // clears the prompt, which re-runs this effect and flushes normally —
      // so the hold is always bounded by an action the user already has.
      // See docs/specs/mid-turn-steering.md ("Paused turns").
      if (this.steering.shouldHoldQueue(this.sessionId())) return;
      const [next, ...rest] = untracked(() => this.queuedMessages());
      if (!next) return;
      // Consume the edge before emitting: the next message waits for the next
      // turn to start and finish. If the parent never starts one, the follow-up
      // stays visible and removable rather than being fired into a rejection.
      this.turnInFlight = false;
      this.queuedMessages.set(rest);
      this.messageSubmitted.emit({ ...next, timestamp: new Date() });
    });
  }

  private focusInput(): void {
    if (this.autoFocus()) {
      this.messageInput()?.nativeElement.focus();
    }
  }

  /**
   * Enter (and the send affordance when idle). While a response is streaming
   * this **queues** rather than stopping: Enter always means "say this".
   *
   * Stopping stays on the button, deliberately. Making Enter ambiguous — send
   * when idle, abort when busy — is what let a reflex keystroke kill a run the
   * user was waiting on, and stopping is the rarer, more destructive of the two.
   *
   * `queueHeld()` is the second reason to queue, and it is NOT covered by
   * `isLoading()`: a turn paused for consent or approval has already closed its
   * stream, so loading is false while the prompt sits there waiting. Gating on
   * loading alone sent the follow-up as a brand-new turn — abandoning the
   * paused turn the user was mid-answer on — while the placeholder promised it
   * would go in when they answered. See docs/specs/mid-turn-steering.md
   * ("Paused turns").
   */
  onSubmit() {
    if (this.isLoading() || this.queueHeld()) {
      this.queueChatRequest();
    } else {
      this.submitChatRequest();
    }
  }

  /**
   * The round button on the right. Unlike Enter it keeps its old meaning while
   * streaming — it is the only Stop affordance, and taking that away to make
   * room for a second Send would leave a user with text typed unable to stop.
   */
  onPrimaryButtonClick() {
    if (this.isLoading()) {
      this.cancelChatRequest();
    } else {
      // Not streaming, so this is Send — but it must make the same decision
      // Enter does, or the two disagree while a prompt is holding the queue.
      this.onSubmit();
    }
  }

  /**
   * Capture the composer's contents as a pending follow-up and clear it, so the
   * user can keep typing. Mirrors `submitChatRequest`'s validation exactly —
   * anything that would not have been sendable is not queueable either.
   */
  private queueChatRequest(): void {
    const content = this.userInput().trim();
    const fileUploadIds = this.readyUploadIds();

    if (!content && fileUploadIds.length === 0) {
      return;
    }

    if (this.hasActivePendingUploads()) {
      this.toastService.warning('Upload in Progress', 'Please wait for file uploads to complete.');
      return;
    }

    const entry: QueuedMessage = {
      id: uuidv4(),
      content,
      fileUploadIds: fileUploadIds.length > 0 ? [...fileUploadIds] : undefined,
      mentionAgentId: this.mentionedAgent()?.agentId,
      invokedSkillIds: this.invokedSkillIds(),
    };
    this.queuedMessages.update(queue => [...queue, entry]);

    // Mid-turn steering: try to land this inside the running turn rather than
    // after it. Fire-and-forget — the queue entry is already visible and the
    // end-of-turn flush already covers it, so nothing here needs to be awaited
    // and no failure needs to be surfaced.
    void this.armSteering(entry);

    // Clear exactly what a real send clears — the queued copy already owns the
    // upload ids, so releasing them here is what lets the next message attach
    // its own files.
    this.userInput.set('');
    this.mentionedAgent.set(null);
    this.closeMentionMenu();
    this.closeSkillMenu();
    this.resetTextareaHeight();
    this.fileUploadService.clearReadyUploads();
  }

  /**
   * Ask the backend to inject this follow-up at the running turn's next tool
   * boundary. See docs/specs/mid-turn-steering.md.
   *
   * Text only, no `@`-mention and no `/` skill command. An injection is a text block
   * appended to the tool-result message, so it cannot carry file attachments; a mention
   * picks the Agent that runs a *turn*, which a mid-turn injection cannot change; and a
   * slash command's directive rides the turn's user message, which by then is already
   * sent. All three must go as a normal turn, and skipping the round trip here is
   * what makes that automatic rather than a backend rejection.
   */
  private async armSteering(entry: QueuedMessage): Promise<void> {
    const sessionId = this.sessionId();
    if (!sessionId) return;
    if (entry.fileUploadIds?.length || entry.mentionAgentId) return;
    // A slash command has to go as a normal turn too: the directive it produces is
    // appended to the turn's *user message*, and a mid-turn injection lands as a text
    // block on the tool-result message of a turn whose skills were already resolved.
    if (entry.invokedSkillIds?.length) return;
    // A paused turn released its lease when the stream closed, so there is no
    // inbox to arm against. This entry rides the resume request instead.
    if (this.queueHeld()) return;

    const armed = await this.steering.arm(sessionId, entry.id, entry.content);
    if (!armed) return;

    // Re-read rather than closing over the entry: the user may have removed it
    // while the request was in flight, in which case it must stay removed (the
    // withdrawal below already raced us to the backend).
    this.queuedMessages.update(queue =>
      queue.map(q => (q.id === entry.id ? { ...q, armed: true } : q)),
    );
  }

  /** Drop a pending follow-up before it is sent. */
  removeQueuedMessage(index: number): void {
    const entry = this.queuedMessages()[index];
    const sessionId = this.sessionId();
    // Withdraw unconditionally when we have a session: the arm may still be in
    // flight, so "not armed yet" is not the same as "nothing to withdraw", and
    // the endpoint is idempotent by design.
    if (entry && sessionId) {
      void this.steering.withdraw(sessionId, entry.id);
    }
    this.queuedMessages.update(queue => queue.filter((_, i) => i !== index));
  }

  submitChatRequest() {
    const content = this.userInput().trim();
    const fileUploadIds = this.readyUploadIds();

    // Must have content or files to submit
    if (!content && fileUploadIds.length === 0) {
      return;
    }

    // Don't submit while uploads are in progress
    if (this.hasActivePendingUploads()) {
      this.toastService.warning('Upload in Progress', 'Please wait for file uploads to complete.');
      return;
    }

    // Emit the message - parent is responsible for managing loading state
    this.messageSubmitted.emit({
      content,
      timestamp: new Date(),
      fileUploadIds: fileUploadIds.length > 0 ? fileUploadIds : undefined,
      mentionAgentId: this.mentionedAgent()?.agentId,
      invokedSkillIds: this.invokedSkillIds(),
    });

    // Clear input and pending uploads. The mention clears with them: it belongs to the
    // turn that was just sent, not to the composer (D11). Invoked skills need no clearing
    // — they are derived from the text, so emptying the text un-invokes them.
    this.userInput.set('');
    this.mentionedAgent.set(null);
    this.closeMentionMenu();
    this.closeSkillMenu();
    this.resetTextareaHeight();
    this.fileUploadService.clearReadyUploads();
  }

  cancelChatRequest() {
    this.messageCancelled.emit();
  }

  async toggleVoice() {
    if (this.isVoiceActive()) {
      await this.voiceChatService.disconnect();
    } else {
      try {
        await this.voiceChatService.connect(this.sessionId() || undefined);
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Failed to start voice';
        this.toastService.error('Voice Error', msg);
      }
    }
  }

  async onFileSelect(event: Event) {
    const input = event.target as HTMLInputElement;
    if (!input.files || input.files.length === 0) {
      return;
    }

    await this.processFiles(Array.from(input.files));

    // Reset input to allow re-selecting same file
    input.value = '';
  }

  /**
   * Handle file removal from pending uploads
   */
  onFileRemove(uploadId: string): void {
    this.fileUploadService.clearPendingUpload(uploadId);
  }

  /**
   * Handle retry for failed uploads
   */
  async onFileRetry(pendingUpload: PendingUpload): Promise<void> {
    const sessionId = this.sessionId();
    if (!sessionId) {
      return;
    }

    // Clear the failed upload
    this.fileUploadService.clearPendingUpload(pendingUpload.uploadId);

    // Retry the upload
    try {
      await this.fileUploadService.uploadFile(sessionId, pendingUpload.file);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Retry failed';
      this.toastService.error('Retry Failed', message);
    }
  }

  onTextareaInput(event: Event) {
    this.settleHints();
    const textarea = event.target as HTMLTextAreaElement;
    this.userInput.set(textarea.value);
    this.autoResize(textarea);
    this.syncMentionToken(textarea);
    this.syncSkillToken(textarea);
  }

  // ---------------------------------------------------------------- mentions (D11)

  /**
   * Recompute the `@…` token from the text before the caret.
   *
   * Matched rather than tracked: the caret can move by click, arrow key, undo or paste,
   * and a state machine that only listens to typing gets out of step with all four. The
   * token must start a word (`^` or whitespace) so an email address never opens the menu,
   * and it ends at whitespace or a second `@`.
   */
  private syncMentionToken(textarea: HTMLTextAreaElement): void {
    if (!this.showAgentMentions()) {
      return;
    }
    const caret = textarea.selectionStart ?? textarea.value.length;
    const before = textarea.value.slice(0, caret);
    const match = /(?:^|\s)@([^\s@]*)$/.exec(before);

    if (!match) {
      this.mentionToken.set(null);
      return;
    }

    void this.mentionService.load();
    const next: MentionToken = { query: match[1], start: caret - match[1].length - 1 };

    // Reset the highlight only when the token itself changed. This runs on `keyup` too,
    // and arrow keys are `preventDefault`ed in `onKeyDown` — so an unconditional reset
    // here would drag the selection back to the first row on the keyup of every
    // ArrowDown, making the menu impossible to walk.
    const current = this.mentionToken();
    if (!current || current.query !== next.query || current.start !== next.start) {
      this.mentionActiveIndex.set(0);
    }
    this.mentionToken.set(next);
  }

  /** Caret moves that are not edits — a click or an arrow key — also open or close the menu. */
  onTextareaCaretMove(event: Event): void {
    const textarea = event.target as HTMLTextAreaElement;
    this.syncMentionToken(textarea);
    this.syncSkillToken(textarea);
  }

  /**
   * Commit a pick: replace the typed `@query` with the Agent's name and remember it for
   * this turn.
   *
   * The literal `@Name` stays in the message text. It is what the user typed, it is what
   * the thread will show them tomorrow when they wonder why one answer looks different,
   * and the model reads it as the address it is.
   */
  onMentionPicked(agent: MentionableAgent): void {
    const token = this.mentionToken();
    const textarea = this.messageInput()?.nativeElement;
    if (!token || !textarea) {
      return;
    }

    const caret = textarea.selectionStart ?? textarea.value.length;
    const replacement = `@${agent.name} `;
    const next =
      textarea.value.slice(0, token.start) + replacement + textarea.value.slice(caret);

    this.userInput.set(next);
    this.mentionedAgent.set(agent);
    this.mentionToken.set(null);

    // Write through to the element and restore the caret: the textarea is not bound to
    // the signal (it uses `[value]` + an input handler), so the DOM is authoritative for
    // the caret and would otherwise sit at the end of the replaced text.
    textarea.value = next;
    const caretAfter = token.start + replacement.length;
    textarea.setSelectionRange(caretAfter, caretAfter);
    textarea.focus();
    this.autoResize(textarea);
  }

  /** Clear the pending mention; the turn goes back to plain chat. */
  clearMention(): void {
    this.mentionedAgent.set(null);
    this.focusInput();
  }

  private closeMentionMenu(): void {
    this.mentionToken.set(null);
  }

  private moveMentionSelection(delta: number): void {
    const count = this.mentionResults().length;
    if (count === 0) {
      return;
    }
    const next = (this.mentionActiveIndex() + delta + count) % count;
    this.mentionActiveIndex.set(next);
  }

  private commitActiveMention(): void {
    const agent = this.mentionResults()[this.mentionActiveIndex()];
    if (agent) {
      this.onMentionPicked(agent);
    }
  }

  // ---------------------------------------------------------------- `/` skill commands

  /**
   * Recompute the `/…` token from the text before the caret.
   *
   * Matched rather than tracked, for the same reason as the `@` token: the caret can move
   * by click, arrow key, undo or paste, and a state machine that only listens to typing
   * gets out of step with all four.
   *
   * The token rule is what keeps `/` usable as ordinary punctuation. It must start a word,
   * so `and/or`, `24/7`, `https://x` and `src/app/foo` never open the menu; it ends at
   * whitespace or a second `/`, so a path that *does* start a word (`/usr/bin`) closes the
   * menu the moment the second slash arrives; and the body is restricted to slug
   * characters, so `/what?` is prose.
   */
  private syncSkillToken(textarea: HTMLTextAreaElement): void {
    if (!this.showSkillCommands()) {
      return;
    }
    const caret = textarea.selectionStart ?? textarea.value.length;
    const before = textarea.value.slice(0, caret);
    const match = /(?:^|\s)\/([a-zA-Z0-9-]*)$/.exec(before);

    if (!match) {
      this.skillToken.set(null);
      return;
    }

    void this.skillCommandService.load();
    const next: MentionToken = { query: match[1], start: caret - match[1].length - 1 };

    // Reset the highlight only when the token itself changed — same reason as the `@`
    // menu: this runs on `keyup`, and an unconditional reset would drag the selection
    // back to the first row on the keyup of every ArrowDown.
    const current = this.skillToken();
    if (!current || current.query !== next.query || current.start !== next.start) {
      this.skillActiveIndex.set(0);
    }
    this.skillToken.set(next);
  }

  /**
   * Commit a pick: replace the typed `/query` with the skill's full command.
   *
   * The literal `/slug` stays in the message. It is what the user typed, it is what the
   * thread will show them tomorrow when they wonder why one answer followed a recipe, and
   * — because the invoked set is derived from the text — it is also the binding itself.
   */
  onSkillPicked(command: SkillCommand): void {
    const token = this.skillToken();
    const textarea = this.messageInput()?.nativeElement;
    if (!token || !textarea) {
      return;
    }

    const caret = textarea.selectionStart ?? textarea.value.length;
    const replacement = `/${command.slug} `;
    const next =
      textarea.value.slice(0, token.start) + replacement + textarea.value.slice(caret);

    this.userInput.set(next);
    this.skillToken.set(null);

    // Write through to the element and restore the caret: the textarea is not bound to
    // the signal (it uses `[value]` + an input handler), so the DOM is authoritative for
    // the caret and would otherwise sit at the end of the replaced text.
    textarea.value = next;
    const caretAfter = token.start + replacement.length;
    textarea.setSelectionRange(caretAfter, caretAfter);
    textarea.focus();
    this.autoResize(textarea);
  }

  /**
   * Clear one invoked skill.
   *
   * The command lives in the text, so un-invoking edits the text — there is no separate
   * binding to drop. That is the point of deriving the set from the message: the chip and
   * what gets sent cannot disagree.
   */
  clearSkillCommand(command: SkillCommand): void {
    const textarea = this.messageInput()?.nativeElement;
    const next = removeSkillCommand(this.userInput(), command.slug);
    this.userInput.set(next);
    if (textarea) {
      textarea.value = next;
      this.autoResize(textarea);
    }
    this.focusInput();
  }

  private closeSkillMenu(): void {
    this.skillToken.set(null);
  }

  private moveSkillSelection(delta: number): void {
    const count = this.skillResults().length;
    if (count === 0) {
      return;
    }
    this.skillActiveIndex.set((this.skillActiveIndex() + delta + count) % count);
  }

  private commitActiveSkill(): void {
    const command = this.skillResults()[this.skillActiveIndex()];
    if (command) {
      this.onSkillPicked(command);
    }
  }

  /** The menu's last row: Customize → Skills, which is where turning one on belongs. */
  onSkillBrowseAll(): void {
    this.closeSkillMenu();
    void this.router.navigate(['/customize/skills']);
  }

  /**
   * Grow the textarea with its content up to MAX_TEXTAREA_HEIGHT_PX, past which
   * it scrolls internally (the template sets overflow-y-auto). Without the clamp
   * the inline height keeps growing past max-height and the scrollbar never
   * becomes usable.
   */
  private autoResize(textarea: HTMLTextAreaElement): void {
    textarea.style.height = 'auto';
    const height = Math.min(textarea.scrollHeight, MAX_TEXTAREA_HEIGHT_PX);
    textarea.style.height = `${height}px`;
  }

  /** Collapse the textarea back to a single row (after submit or clear). */
  private resetTextareaHeight(): void {
    const textarea = this.messageInput()?.nativeElement;
    if (!textarea) {
      return;
    }
    textarea.style.height = `${MIN_TEXTAREA_HEIGHT_PX}px`;
    textarea.scrollTop = 0;
  }

  onKeyDown(event: KeyboardEvent) {
    // Any key at all — including the arrows and Escape a menu consumes below —
    // means the user is working, not reading hints.
    this.settleHints();

    // The `@` menu owns the keyboard while it is open (D11). Enter must pick an Agent
    // rather than send the half-typed message — a send that fires out from under an open
    // menu is the single most annoying way to get an autocomplete wrong.
    if (this.isMentionMenuOpen()) {
      switch (event.key) {
        case 'ArrowDown':
          event.preventDefault();
          this.moveMentionSelection(1);
          return;
        case 'ArrowUp':
          event.preventDefault();
          this.moveMentionSelection(-1);
          return;
        case 'Enter':
        case 'Tab':
          if (this.mentionResults().length > 0) {
            event.preventDefault();
            this.commitActiveMention();
            return;
          }
          break;
        case 'Escape':
          event.preventDefault();
          this.closeMentionMenu();
          return;
      }
    }

    // The `/` menu owns the keyboard on exactly the same terms, and only when the `@`
    // menu is not already claiming it (`isSkillMenuVisible`).
    if (this.isSkillMenuVisible()) {
      switch (event.key) {
        case 'ArrowDown':
          event.preventDefault();
          this.moveSkillSelection(1);
          return;
        case 'ArrowUp':
          event.preventDefault();
          this.moveSkillSelection(-1);
          return;
        case 'Enter':
        case 'Tab':
          if (this.skillResults().length > 0) {
            event.preventDefault();
            this.commitActiveSkill();
            return;
          }
          break;
        case 'Escape':
          event.preventDefault();
          this.closeSkillMenu();
          return;
      }
    }

    // Submit on Enter (without Shift)
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      this.onSubmit();
    }
  }

  onFocus() {
    this.isFocused.set(true);
    // Warm the `@` candidates before the first keystroke needs them — both sources are
    // session-cached, so this costs one request per session and makes the menu instant.
    if (this.showAgentMentions()) {
      void this.mentionService.load();
    }
    if (this.showSkillCommands()) {
      void this.skillCommandService.load();
    }
  }

  onBlur() {
    this.isFocused.set(false);
    // The menu's own rows commit on `mousedown` and preventDefault, so reaching here
    // means the user went somewhere else entirely — close it. The *selected* Agent
    // survives: it belongs to the pending turn, not to the menu. So do the skill
    // commands, which live in the message text rather than in the menu.
    this.closeMentionMenu();
    this.closeSkillMenu();
  }

  /** The menu's last row: the store, which is where a search over everything belongs (D11). */
  onMentionBrowseAll(): void {
    this.closeMentionMenu();
    void this.router.navigate(['/agents/discover']);
  }

  // =========================================================================
  // Drag and Drop Handlers
  // =========================================================================

  onDragEnter(event: DragEvent): void {
    event.preventDefault();
    event.stopPropagation();
    this.dragCounter++;

    // Check if dragging files
    if (event.dataTransfer?.types.includes('Files')) {
      this.isDraggingOver.set(true);
    }
  }

  onDragOver(event: DragEvent): void {
    event.preventDefault();
    event.stopPropagation();

    // Set the drop effect
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = 'copy';
    }
  }

  onDragLeave(event: DragEvent): void {
    event.preventDefault();
    event.stopPropagation();
    this.dragCounter--;

    // Only hide overlay when truly leaving the dropzone
    if (this.dragCounter === 0) {
      this.isDraggingOver.set(false);
    }
  }

  async onDrop(event: DragEvent): Promise<void> {
    event.preventDefault();
    event.stopPropagation();

    // Reset drag state
    this.dragCounter = 0;
    this.isDraggingOver.set(false);

    const files = event.dataTransfer?.files;
    if (!files || files.length === 0) {
      return;
    }

    // Process dropped files using the same logic as file select
    await this.processFiles(Array.from(files));
  }

  /**
   * Process files for upload (shared by file input and drag-drop)
   */
  private async processFiles(newFiles: File[]): Promise<void> {
    // Emit fileAttached for each file FIRST to trigger session creation if needed
    for (const file of newFiles) {
      this.fileAttached.emit(file);
    }

    // Wait a tick for Angular to process the signal update from parent
    await new Promise(resolve => setTimeout(resolve, 0));

    // Now get the session ID (should be available after parent creates staged session)
    const sessionId = this.sessionId();
    if (!sessionId) {
      this.toastService.error('Upload Error', 'Failed to create session for file upload.');
      return;
    }

    // Check file count limit
    const currentCount = this.pendingUploads().length;
    if (currentCount + newFiles.length > MAX_FILES_PER_MESSAGE) {
      this.toastService.warning(
        'File Limit',
        `Maximum ${MAX_FILES_PER_MESSAGE} files per message. You have ${currentCount} already attached.`
      );
      return;
    }

    // Nudge the user once per batch if they're attaching tabular files
    // without the Spreadsheet Analysis tool enabled — the backend routes
    // these to the tool instead of inline Bedrock document blocks (#206),
    // so the user needs the tool enabled to get answers about the data.
    let tabularNudgeShown = false;

    // Validate and upload each file
    for (const file of newFiles) {
      // Check file size (pptx has its own, larger cap — see maxFileSizeFor)
      const sizeLimit = maxFileSizeFor(file);
      if (file.size > sizeLimit) {
        this.toastService.error(
          'File Too Large',
          `${file.name} exceeds maximum size of ${formatBytes(sizeLimit)}.`
        );
        continue;
      }

      // Check file type
      const ext = file.name.substring(file.name.lastIndexOf('.')).toLowerCase();
      if (!ALLOWED_EXTENSIONS.includes(ext)) {
        this.toastService.error(
          'Invalid File Type',
          `${file.name} is not a supported file type. Allowed: ${ALLOWED_EXTENSIONS.join(', ')}`
        );
        continue;
      }

      if (!tabularNudgeShown && this.isTabularFile(file)) {
        const enabled = this.toolService
          .enabledToolIds()
          .includes('analyze_spreadsheet');
        if (!enabled) {
          this.toastService.info(
            'Enable Spreadsheet Analysis',
            'To analyze spreadsheets, enable "Spreadsheet Analysis" under Customize → Tools in the sidebar.'
          );
          tabularNudgeShown = true;
        }
      }

      // Upload file
      try {
        await this.fileUploadService.uploadFile(sessionId, file);
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Upload failed';
        this.toastService.error('Upload Failed', `${file.name}: ${message}`);
      }
    }
  }

  private isTabularFile(file: File): boolean {
    const tabularExts = ['.csv', '.xls', '.xlsx'];
    const tabularMimes = [
      'text/csv',
      'application/vnd.ms-excel',
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    ];
    const lower = file.name.toLowerCase();
    if (tabularExts.some(ext => lower.endsWith(ext))) return true;
    return tabularMimes.includes((file.type || '').toLowerCase());
  }
}