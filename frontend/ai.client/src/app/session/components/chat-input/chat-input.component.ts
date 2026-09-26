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
  DestroyRef,
  ElementRef,
  Injector,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { v4 as uuidv4 } from 'uuid';
import { Router } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroPlus,
  heroArrowTurnDownRight,
  heroCheck,
  heroClock,
  heroMicrophone,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import { heroArrowUpSolid, heroStopSolid } from '@ng-icons/heroicons/solid';
import { ModelDropdownComponent } from '../../../components/model-dropdown/model-dropdown.component';
import { AnnouncementBannerComponent } from '../../../components/announcement-banner/announcement-banner.component';
import { QuotaWarningBannerComponent } from '../../../components/quota-warning-banner/quota-warning-banner.component';
import { AgentNoticeBannerComponent } from '../agent-notice-banner/agent-notice-banner.component';
import { TooltipDirective } from '../../../components/tooltip';
import { FileCardComponent } from '../../../components/file-card';
import { StorageQuotaBannerComponent } from '../../../components/storage-quota-banner';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';
import {
  FileUploadService,
  FileMetadata,
  PendingUpload,
  ALLOWED_EXTENSIONS,
  maxFileSizeFor,
  MAX_FILES_PER_MESSAGE,
  formatBytes
} from '../../../services/file-upload';
import { ToastService } from '../../../services/toast/toast.service';
import { ToolService } from '../../../services/tool/tool.service';
import { VoiceChatService, type VoiceStatus } from '../../services/voice';
import {
  DictationService,
  DictationUnavailableError,
  type DictationEndReason,
} from '../../services/dictation';
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
} from '../../../services/skill/skill-command.service';
import { SkillCommandMenuComponent } from './skill-command-menu.component';
import { SteeringService } from '../../services/chat/steering.service';
import { ComposerDraftService } from '../../services/session/composer-draft.service';
import {
  ComposerDraftStorageService,
  EMPTY_DRAFT,
  StoredAttachment,
  StoredComposerDraft,
} from '../../services/session/composer-draft-storage.service';
import { ComposerHandoffService } from './composer-handoff.service';
import { ComposerSegment, findHighlightRanges, toSegments } from './composer-highlights';

// Must stay in sync with the min-height/max-height classes on the textarea in
// chat-input.component.html (`textareaClass`).
const MIN_TEXTAREA_HEIGHT_PX = 60;
const COMPACT_MIN_TEXTAREA_HEIGHT_PX = 40;
const MAX_TEXTAREA_HEIGHT_PX = 200;

/**
 * A compact textarea taller than this has wrapped onto a second line: one
 * 24px line plus 8px padding top and bottom is 40, and the slack absorbs
 * sub-pixel rounding without admitting a second line.
 */
const COMPACT_SINGLE_LINE_MAX_PX = 44;

/** How long the shell takes to change height when the composer folds, unfolds or docks. */
const SHELL_RESIZE_MS = 200;

/** The composer's resting placeholder, and the string the rotation settles back on. */
const IDLE_PLACEHOLDER = 'How can I help you today?';

/**
 * Where dictated text goes: the composer's text either side of the caret (or
 * selection, which dictation replaces) when the user pressed Dictate.
 */
export interface DictationAnchor {
  before: string;
  after: string;
}

/**
 * Splice dictated text into the composer at its anchor, padding with a space
 * only where the neighbouring text does not already supply whitespace.
 * Returns the new value and where the caret belongs — just after the insert.
 */
export function spliceDictation(
  anchor: DictationAnchor,
  dictated: string,
): { value: string; caret: number } {
  const text = dictated.trim();
  if (!text) {
    return { value: anchor.before + anchor.after, caret: anchor.before.length };
  }
  const lead = anchor.before && !/\s$/.test(anchor.before) ? ' ' : '';
  const trail = anchor.after && !/^\s/.test(anchor.after) ? ' ' : '';
  const head = anchor.before + lead + text;
  return { value: head + trail + anchor.after, caret: head.length };
}

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
  /**
   * Display metadata for `fileUploadIds`, captured at queue time.
   *
   * Queueing releases the composer's attachments so the next follow-up can
   * attach its own, which means the ids on this entry outlive the only two
   * places their filename and size could be read from. Without this copy a
   * queued question restored after a reload comes back without the file it
   * was about.
   */
  attachments?: StoredAttachment[];
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

/**
 * A restored card back into the shape storage keeps.
 *
 * The composer holds restored attachments as `FileMetadata` — the card's own
 * server-side shape — so the template binds them directly instead of mapping
 * on every change detection pass. Only the four display fields are stored;
 * `s3Uri`, `createdAt` and `status` come back from the reconcile, and nothing
 * reads them before it lands.
 */
function toStoredAttachment(file: FileMetadata): StoredAttachment {
  return {
    uploadId: file.uploadId,
    filename: file.filename,
    mimeType: file.mimeType,
    sizeBytes: file.sizeBytes,
  };
}

/** The cached half of a `FileMetadata`, good enough to paint a card with. */
function toFileMetadata(attachment: StoredAttachment, sessionId: string): FileMetadata {
  return {
    uploadId: attachment.uploadId,
    filename: attachment.filename,
    mimeType: attachment.mimeType,
    sizeBytes: attachment.sizeBytes,
    sessionId,
    s3Uri: '',
    status: 'ready',
    createdAt: '',
  };
}

/**
 * First mention of each upload id wins, so an attachment held by both the
 * composer and a queued follow-up is stored (and restored) once.
 */
function dedupeAttachments(attachments: StoredAttachment[]): StoredAttachment[] {
  const seen = new Set<string>();
  return attachments.filter(attachment => {
    if (seen.has(attachment.uploadId)) return false;
    seen.add(attachment.uploadId);
    return true;
  });
}

@Component({
  selector: 'app-chat-input',
  imports: [AgentNoticeBannerComponent, AnnouncementBannerComponent, FormsModule, ModelDropdownComponent, NgIcon, QuotaWarningBannerComponent, StorageQuotaBannerComponent, TooltipDirective, FileCardComponent, AgentMentionMenuComponent, SkillCommandMenuComponent, SpinnerComponent],
  // `relative` is the anchor the announcement banner floats against — it sits
  // `bottom-full` of this host, above the quota tabs and clear of the composer.
  host: { class: 'relative block' },
  providers: [
    provideIcons({
      heroPlus,
      heroArrowTurnDownRight,
      heroCheck,
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
  private readonly draftStorage = inject(ComposerDraftStorageService);
  private readonly toolService = inject(ToolService);
  private readonly voiceChatService = inject(VoiceChatService);
  private readonly dictation = inject(DictationService);
  protected readonly systemPromptsService = inject(SystemPromptsService);
  private readonly router = inject(Router);
  private readonly handoff = inject(ComposerHandoffService);
  private readonly injector = inject(Injector);
  private readonly destroyRef = inject(DestroyRef);

  // Input: session ID for file uploads
  readonly sessionId = input<string | null>(null);

  // Input: loading state (required - parent must provide this)
  readonly isChatLoading = input<boolean>(false);

  // Input: show file attachment controls (defaults to true)
  readonly showFileControls = input<boolean>(true);

  // Input: show voice mode toggle (defaults to true). Disabled where voice
  // is not meaningful, e.g. the assistant editor preview.
  readonly showVoiceControl = input<boolean>(true);

  // Input: show the Dictate (speech-to-text) button (defaults to true). Unlike
  // voice, dictation only fills this composer, so it stays on in the preview
  // panes; the input is here for an embedder that has no use for it.
  readonly showDictationControl = input<boolean>(true);

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

  /**
   * Which conversation's unsent text this composer parks and takes back, or
   * `null` (the default) to remember nothing.
   *
   * Deliberately not `sessionId`. That input is the id file uploads attach
   * to, which a new conversation mints the moment a file is staged — keying
   * drafts off it would blank the composer the instant someone attached a
   * file to text they had already typed. The container passes the *route*
   * conversation, or `NEW_CONVERSATION_DRAFT_KEY` for one not yet sent.
   *
   * Opt-in rather than opt-out, unlike the `show*` controls above: an
   * embedded preview pane is a throwaway, and text left in one should not
   * come back the next time it is opened.
   */
  readonly draftKey = input<string | null>(null);

  /**
   * The conversation layout: one row — attach, text, speech controls — with
   * the model picker and the cost/context line moved beneath the shell. False
   * (the default) is the roomy empty-state composer, which keeps the picker in
   * its own bar. The container sets this once the conversation has messages.
   */
  readonly compact = input<boolean>(false);

  private readonly messageInput = viewChild<ElementRef<HTMLTextAreaElement>>('messageInput');
  private readonly shell = viewChild<ElementRef<HTMLElement>>('shell');
  private readonly highlightMirror = viewChild<ElementRef<HTMLElement>>('highlightMirror');

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

  /**
   * The draft key this composer is currently mirroring, or `undefined` before
   * it has adopted one.
   *
   * The three states are distinct and all load-bearing: `undefined` means
   * there is no outgoing draft to park (first mount), `null` means
   * persistence is off for this placement, and a string is a conversation
   * whose text must be written back before the composer adopts another's.
   */
  private mirroredDraftKey: string | null | undefined = undefined;

  /**
   * An `@`-mention restored from a draft, waiting for the candidate list to
   * load so it can be re-bound to a current row. Cleared on the first resolve
   * attempt with a non-empty list — a hit binds, a miss drops it, and both are
   * final so a later list change cannot resurrect a mention the user has since
   * removed.
   */
  private readonly pendingMentionAgentId = signal<string | null>(null);

  // Output events
  fileAttached = output<File>();
  messageSubmitted = output<Message>();
  messageCancelled = output<void>();

  // File upload state from service
  readonly pendingUploads = this.fileUploadService.pendingUploadsList;
  readonly hasActivePendingUploads = this.fileUploadService.hasActivePendingUploads;
  readonly readyUploadIds = this.fileUploadService.readyUploadIds;

  /**
   * Files attached in an earlier visit to this conversation, rebuilt from the
   * stored draft.
   *
   * Metadata, not an upload: the bytes are already in S3 and the id is all the
   * send needs, so there is nothing to re-upload and no `File` to hold. They
   * render through `app-file-card`'s existing `[file]` input — the same shape
   * the file browser passes it — which is why restoring one costs no changes
   * to the card.
   *
   * Painted straight from storage so the cards are there on first frame, then
   * reconciled against the server (see `reconcileRestoredAttachments`), which
   * is what makes a stale or hand-edited id disappear instead of failing at
   * send time.
   */
  readonly restoredAttachments = signal<FileMetadata[]>([]);

  /**
   * Every upload id this message would carry: restored first, then the ones
   * uploaded in this sitting, so the order matches the order they were
   * attached. Both send paths read this rather than `readyUploadIds`, or a
   * restored attachment would show a card and then not travel.
   */
  readonly attachmentIds = computed(() => [
    ...new Set([
      ...this.restoredAttachments().map(attachment => attachment.uploadId),
      ...this.readyUploadIds(),
    ]),
  ]);

  // Computed: show file attachments area
  readonly showFileAttachments = computed(
    () => this.pendingUploads().length > 0 || this.restoredAttachments().length > 0,
  );

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
    // Shown only until the first words arrive; after that the transcript is
    // the textarea's value.
    if (this.isDictating()) {
      return this.dictationStatus() === 'connecting' ? 'Starting dictation…' : 'Listening…';
    }
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
  // Layout
  //
  // There is no send button: Enter sends, and a button nobody clicked was
  // costing a row of height on every conversation page. What the button also
  // did has to live somewhere else, and each piece has its own home:
  //
  // - **Stop** takes voice mode's slot while a response streams (and Escape
  //   stops, when no menu or dictation is using the key).
  // - **Send on touch.** A phone has no Enter a user thinks of as "send", so on
  //   a coarse pointer return adds a line and a send button appears, in the
  //   same slot, only once there is something to send.
  // - **Why Enter did nothing** — an upload still in flight — is said in words
  //   on the status line under the shell, where the disabled button used to
  //   say it by being grey.
  //
  // Compact is one row while the draft fits on one line. Once it wraps, the
  // text takes the full width and the buttons drop to a bar beneath it (the
  // empty state's shape), so a long draft is not framed by two columns of dead
  // space. It folds back only when the draft is empty again: folding at the
  // wrap point would flip the layout back and forth on every keystroke that
  // crossed it.
  // =========================================================================

  /** A compact draft that wrapped and took the full width. */
  protected readonly unfolded = signal(false);

  /** Text on top, controls in a bar beneath: the empty state, or a compact draft that wrapped. */
  protected readonly stacked = computed(() => !this.compact() || this.unfolded());

  /**
   * Whether the primary pointer is a finger. Read live rather than once: a
   * tablet that gains a trackpad, or devtools' device toggle, flips it.
   */
  protected readonly isCoarsePointer = signal(false);

  /** There is something a send would carry. */
  private readonly hasDraft = computed(
    () => this.userInput().trim().length > 0 || this.attachmentIds().length > 0,
  );

  /** Stop, in voice mode's slot. Voice runs its own stop inside the overlay. */
  protected readonly showStop = computed(
    () => this.isLoading() && !this.isVoiceActive() && !this.isDictating(),
  );

  /**
   * Send, for touch only. Makes the same decision Enter does (`onSubmit`), so
   * mid-stream it queues a follow-up exactly as a keyboard user's Enter would.
   */
  protected readonly showTouchSend = computed(
    () => this.isCoarsePointer() && this.hasDraft() && !this.isDictating(),
  );

  /** Voice mode keeps its slot unless Stop or touch Send needs it — or voice is live, to end it. */
  protected readonly showVoiceSlot = computed(
    () =>
      this.showVoiceControl() &&
      (this.isVoiceActive() || (!this.showStop() && !this.showTouchSend())),
  );

  /**
   * One short line on the status side of the meta line, standing in for cost
   * and context while it applies. Only for the states where the composer is
   * doing something the user did not see coming — a queue held behind a prompt
   * already explains itself in the shelf above the text.
   */
  protected readonly statusHint = computed<string | null>(() => {
    if (this.isDictating()) {
      return this.dictationStatus() === 'connecting'
        ? 'Starting dictation…'
        : 'Enter to insert · Esc to cancel';
    }
    if (this.hasActivePendingUploads()) return 'Enter sends once the upload finishes';
    // Compact only: the empty state's composer streams for a frame or two at
    // most — until the first send swaps it out — and a line flashing in under
    // it for that long reads as a glitch, not a hint.
    if (this.showStop() && this.compact() && !this.isCoarsePointer()) return 'Esc to stop';
    return null;
  });

  /**
   * Whether the line beneath the shell renders. Always in compact — it holds
   * the model picker — and in the empty state only while there is a status to
   * say, since the empty state has no cost yet and keeps its picker in the bar.
   */
  protected readonly showMetaLine = computed(() => this.compact() || this.statusHint() !== null);

  /**
   * Padding shared by the textarea, the highlight mirror behind it and the
   * hint overlay in front of it. The three must wrap identically, or the
   * highlight drifts off its token and the hint off the placeholder.
   */
  protected readonly fieldPadding = computed(() => {
    if (!this.compact()) return 'px-2 py-4';
    return this.unfolded() ? 'px-2 pt-2 pb-1' : 'px-2 py-2';
  });

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
      !this.isDictating() &&
      !this.isLoading() &&
      !this.queueHeld() &&
      this.composerHints().length > 1,
  );

  /**
   * Placeholder colour follows the hint overlay (the two must never both show);
   * text colour goes italic and a step lighter while a dictation preview is
   * showing, so heard-but-not-inserted words read as provisional.
   */
  protected readonly textareaClass = computed(() => {
    const placeholder = this.showHintOverlay()
      ? 'placeholder:text-transparent'
      : 'placeholder:text-gray-500 dark:placeholder:text-gray-400';
    const text = this.isDictating()
      ? 'italic text-gray-600 dark:text-gray-300'
      : 'text-gray-900 dark:text-gray-100';
    const minHeight = this.compact() ? 'min-h-10' : 'min-h-15';
    return `${placeholder} ${text} ${minHeight} ${this.fieldPadding()}`;
  });

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
    const hasReadyFiles = this.attachmentIds().length > 0;
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
  // Dictation
  //
  // Speech-to-text into this composer. While it runs the textarea shows the
  // anchored text with the live transcript spliced in, read-only and in
  // italics; `userInput` is not touched until Done, so a Cancel restores the
  // composer exactly and a draft never captures a half-heard partial.
  //
  // `DictationService` is a root singleton; `ownsDictation` scopes its state
  // to the composer that started it, so an embedded preview composer never
  // renders another composer's dictation.
  // =========================================================================

  private readonly ownsDictation = signal(false);
  private readonly dictationAnchor = signal<DictationAnchor | null>(null);

  protected readonly isDictating = computed(
    () => this.ownsDictation() && this.dictation.isActive(),
  );
  protected readonly dictationStatus = this.dictation.status;
  protected readonly dictationLevels = this.dictation.levels;

  protected readonly showDictateButton = computed(
    () =>
      this.showDictationControl() &&
      this.dictation.isSupported() &&
      !this.dictation.unavailable(),
  );

  /** The live composed text while dictating, or null when not. */
  private readonly dictationPreview = computed(() => {
    const anchor = this.dictationAnchor();
    if (!anchor || !this.isDictating()) return null;
    return spliceDictation(anchor, this.dictation.transcript()).value;
  });

  /** What the textarea shows: the dictation preview while it runs, else the user's text. */
  protected readonly composerText = computed(() => this.dictationPreview() ?? this.userInput());

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
   * spaces, so that would otherwise happen constantly. Deleting the `@Name` is how you
   * change your mind (`dropStaleMention`).
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
   * The draft split into plain and live runs, for the mirror layer painted
   * behind the textarea — or null when nothing in it is live, so the mirror is
   * not rendered at all.
   *
   * This is the whole indicator for `@` and `/` now. A chip beside the input
   * said the same thing as the token already in the text, and cost the compact
   * row its width; a tint on the token itself says it in place. Off while
   * dictating, when the textarea shows a preview rather than `userInput`.
   */
  protected readonly highlightSegments = computed<ComposerSegment[] | null>(() => {
    if (this.isDictating()) return null;
    const text = this.userInput();
    const ranges = findHighlightRanges(
      text,
      this.mentionedAgent()?.name ?? null,
      this.invokedSkills().map((command) => command.slug),
    );
    return toSegments(text, ranges);
  });

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
    // ...and size it, because a draft restored before the view existed set the
    // signal but had no element to grow.
    afterNextRender(() => {
      this.focusInput();
      this.sizeTextareaTo(untracked(this.userInput));
      // A compact composer mounted by the first send: shrink from the empty
      // state's composer rather than appearing at the final size.
      if (untracked(this.compact)) {
        const from = this.handoff.take();
        const shell = this.shell()?.nativeElement;
        if (from && shell) this.animateShellFrom(shell, from);
      }
    });

    if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
      const pointer = window.matchMedia('(pointer: coarse)');
      this.isCoarsePointer.set(pointer.matches);
      const onPointerChange = (event: MediaQueryListEvent) => this.isCoarsePointer.set(event.matches);
      pointer.addEventListener?.('change', onPointerChange);
      this.destroyRef.onDestroy(() => pointer.removeEventListener?.('change', onPointerChange));
    }

    // Fold a wrapped compact draft back to one row once it is empty — after a
    // send, a queue, or the user clearing it. Never earlier: see "Layout".
    effect(() => {
      if (this.unfolded() && this.userInput().length === 0 && !this.isDictating()) {
        untracked(() => this.setUnfolded(false));
      }
    });
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

    // Park the composer's unsent text under its conversation, and take back
    // whatever was parked when the user returns to one.
    //
    // A single effect over both signals, because the two cases have to be
    // told apart: when only the text changed, mirror it; when the
    // conversation changed, the text still belongs to the one being left, so
    // it is written *there* before this composer adopts the new one's draft.
    // Keying the write off the incoming conversation instead would file one
    // thread's half-written question under another's.
    //
    // Every path that empties the composer — send, queue-as-follow-up, the
    // user deleting it — flows through `userInput` and so forgets the draft
    // without naming it. The one exception is send: it also writes at once
    // (`persistDraftNow`), because the first send can destroy this instance
    // before the effect runs again.
    effect(() => {
      const key = this.draftKey();
      const draft = this.composerDraftSnapshot();
      untracked(() => this.syncDraft(key, draft));
    });

    // Re-bind a restored `@`-mention once the candidate list arrives.
    //
    // The draft stores the Agent's id, not the row: a name, icon or tagline
    // that changed since the draft was written must come back current, and an
    // Agent that was deleted or unshared must come back not at all rather than
    // as a stale row the send would reject. The list loads lazily, so this
    // waits for it instead of resolving at adopt time — `pendingMentionAgentId`
    // is what holds the intent across that gap.
    effect(() => {
      const candidates = this.mentionService.mentionable();
      const wanted = this.pendingMentionAgentId();
      if (!wanted || candidates.length === 0) return;
      const match = candidates.find(agent => agent.agentId === wanted);
      untracked(() => {
        this.pendingMentionAgentId.set(null);
        if (match) this.mentionedAgent.set(match);
      });
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
      // Built field by field rather than spread: the entry also carries queue
      // bookkeeping (`id`, `armed`, cached attachment metadata) that is ours,
      // not the conversation's.
      this.messageSubmitted.emit({
        content: next.content,
        timestamp: new Date(),
        fileUploadIds: next.fileUploadIds,
        mentionAgentId: next.mentionAgentId,
        invokedSkillIds: next.invokedSkillIds,
      });
    });

    // Keep the textarea's height tracking the transcript as it grows, and
    // keep the newest words in view.
    effect(() => {
      const preview = this.dictationPreview();
      if (preview === null) return;
      const textarea = this.messageInput()?.nativeElement;
      if (!textarea) return;
      textarea.value = preview;
      this.autoResize(textarea);
      textarea.scrollTop = textarea.scrollHeight;
    });

    // The conversation changed under a running dictation (the route moving
    // from a new chat to its id after a send, or the user switching threads).
    // Re-anchor at the end of the composer that is now showing rather than
    // cancel: the user is mid-sentence, and the old anchor's text has already
    // been parked with its own conversation by the draft effect above.
    effect(() => {
      this.draftKey();
      untracked(() => {
        if (!this.ownsDictation()) return;
        this.dictationAnchor.set({ before: this.userInput(), after: '' });
      });
    });

    inject(DestroyRef).onDestroy(() => this.cancelDictation());
  }

  private focusInput(): void {
    if (this.autoFocus()) {
      this.messageInput()?.nativeElement.focus();
    }
  }

  /**
   * Everything about the composer worth coming back to, as one value the
   * persistence effect can depend on.
   *
   * **Only unarmed follow-ups are captured.** An armed entry is one the
   * backend has confirmed it holds against the running turn, so the user's
   * words are already delivered and restoring them would be the duplicate;
   * an unarmed one has no such confirmation and dies with this component, so
   * it is the one actually at risk. Between those two the ambiguous case —
   * armed while the arm request was still in flight — resolves as a visible
   * duplicate rather than a silent loss, which is the same trade the queue
   * itself makes (see `QueuedMessage.armed`).
   */
  private composerDraftSnapshot(): StoredComposerDraft {
    const queuedEntries = this.queuedMessages().filter(entry => !entry.armed);
    const attachments = [
      ...this.attachmentMetadata(),
      // A queued follow-up carries its own attachments; they are just as unsent
      // as the composer's, and folding the text back without them would send
      // the question without the file it was about.
      ...queuedEntries.flatMap(entry => entry.attachments ?? []),
    ];
    return {
      text: this.userInput(),
      mentionAgentId: this.mentionedAgent()?.agentId ?? this.pendingMentionAgentId() ?? undefined,
      queued: queuedEntries.map(entry => entry.content).filter(content => !!content),
      attachments: dedupeAttachments(attachments),
      attachmentSessionId: this.sessionId() ?? undefined,
    };
  }

  /** Every attachment currently on the composer, in the order it will be sent. */
  private attachmentMetadata(): StoredAttachment[] {
    return dedupeAttachments([
      ...this.restoredAttachments().map(toStoredAttachment),
      ...this.fileUploadService.readyUploads().map(upload => ({
        uploadId: upload.uploadId,
        filename: upload.file.name,
        mimeType: upload.file.type || 'application/octet-stream',
        sizeBytes: upload.file.size,
      })),
    ]);
  }

  /**
   * Mirror the composer into storage, or swap drafts when the conversation
   * changed. See the effect in the constructor.
   */
  private syncDraft(key: string | null, draft: StoredComposerDraft): void {
    if (key === this.mirroredDraftKey) {
      if (key !== null) this.draftStorage.write(key, draft);
      return;
    }
    // `draft` is the outgoing conversation's — park it there first. `undefined`
    // is first mount, where there is nothing to park.
    if (this.mirroredDraftKey) {
      this.draftStorage.write(this.mirroredDraftKey, draft);
      // `FileUploadService` is application-wide, so its pending list is not
      // scoped to a conversation by itself. The line above just filed these
      // under the conversation being left; releasing them here is what stops
      // them following the user into the next one.
      this.fileUploadService.clearReadyUploads();
    }
    this.mirroredDraftKey = key;
    this.adoptDraft(key === null ? EMPTY_DRAFT : this.draftStorage.read(key));
    // Write the adopted state straight back, rather than waiting for the next
    // change. On first mount there is nothing to wait for, and a file staged
    // from the empty-state page before this composer rendered would otherwise
    // never be filed against the conversation at all.
    if (key !== null) this.draftStorage.write(key, this.composerDraftSnapshot());
  }

  /**
   * Write the composer's state under its conversation now, instead of on the
   * draft effect's next run.
   *
   * The first send from the empty state swaps this composer for the compact
   * one, and the swap destroys this instance before its effect runs again. The
   * mirrored draft would then still hold the message just sent, and every later
   * New Session would open with it in the composer.
   */
  private persistDraftNow(): void {
    if (this.mirroredDraftKey) {
      this.draftStorage.write(this.mirroredDraftKey, this.composerDraftSnapshot());
    }
  }

  /**
   * Replace the composer's contents with a conversation's remembered draft
   * (or empty it, when that conversation has none).
   *
   * **Queued follow-ups come back as composer text, not as queued chips.** A
   * chip promises the follow-up goes out when the turn ends, and after a
   * reload there is no turn left to hang that promise on: the flush is
   * edge-triggered on a stream this component never saw start, so a restored
   * chip would sit there forever. Text makes no promise and loses nothing —
   * the user presses Enter and it queues again. They land ahead of the live
   * text because that is the order they were written in.
   *
   * The textarea is written directly as well as through the signal: the
   * `[value]` binding only lands on the next change detection, and the resize
   * below has to measure the new text, not the old.
   */
  private adoptDraft(draft: StoredComposerDraft): void {
    const text = [...(draft.queued ?? []), draft.text]
      .map(part => part.trim())
      .filter(part => !!part)
      .join('\n\n');

    this.userInput.set(text);
    this.mentionedAgent.set(null);
    this.pendingMentionAgentId.set(draft.mentionAgentId ?? null);
    if (draft.mentionAgentId) void this.mentionService.load();
    this.closeMentionMenu();
    this.closeSkillMenu();

    this.restoredAttachments.set(
      (draft.attachments ?? []).map(attachment =>
        toFileMetadata(attachment, draft.attachmentSessionId ?? ''),
      ),
    );
    void this.reconcileRestoredAttachments(draft.attachmentSessionId);

    const textarea = this.messageInput()?.nativeElement;
    if (textarea) textarea.value = text;
    this.sizeTextareaTo(text);
  }

  /**
   * Check restored attachments against the server and drop what it does not
   * confirm.
   *
   * The cards are already on screen by the time this runs, which is the point:
   * storage is a display cache so the composer paints in one frame, and this
   * is what stops that cache outliving the truth. A file deleted from the file
   * browser, an id left over from another account, a hand-edited entry — all
   * resolve to nothing here and the card goes away, rather than travelling
   * with the send and silently resolving to no file server-side.
   *
   * Failure is deliberately a no-op: the listing is owner-scoped and the send
   * re-checks ownership anyway, so a network blip should leave the user's
   * attachments on screen rather than quietly stripping them.
   */
  private async reconcileRestoredAttachments(sessionId: string | undefined): Promise<void> {
    const wanted = new Set(untracked(this.restoredAttachments).map(a => a.uploadId));
    if (!sessionId || wanted.size === 0) return;
    const adoptedFor = this.mirroredDraftKey;
    try {
      const files = await this.fileUploadService.listSessionFiles(sessionId);
      // The conversation moved on while the request was in flight; whatever
      // this answers is about a composer that no longer exists.
      if (this.mirroredDraftKey !== adoptedFor) return;
      const confirmed = files.filter(file => wanted.has(file.uploadId));
      const byId = new Map(confirmed.map(file => [file.uploadId, file]));
      this.restoredAttachments.update(list =>
        list.flatMap(attachment => {
          const server = byId.get(attachment.uploadId);
          return server ? [server] : [];
        }),
      );
    } catch {
      // Leave the cached cards alone — see the note above.
    }
  }

  /** Grow the textarea to `text`, or collapse it to one row when empty. */
  private sizeTextareaTo(text: string): void {
    const textarea = this.messageInput()?.nativeElement;
    if (!textarea) return;
    if (!text) {
      this.resetTextareaHeight();
      return;
    }
    this.autoResize(textarea);
  }

  /**
   * Enter (and the touch Send button). While a response is streaming this
   * **queues** rather than stopping: Enter always means "say this".
   *
   * Stopping has its own controls — the Stop button in voice mode's slot, and
   * Escape — deliberately. Making Enter ambiguous (send when idle, abort when
   * busy) is what let a reflex keystroke kill a run the user was waiting on,
   * and stopping is the rarer, more destructive of the two.
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
   * Capture the composer's contents as a pending follow-up and clear it, so the
   * user can keep typing. Mirrors `submitChatRequest`'s validation exactly —
   * anything that would not have been sendable is not queueable either.
   */
  private queueChatRequest(): void {
    const content = this.userInput().trim();
    const fileUploadIds = this.attachmentIds();

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
      attachments: fileUploadIds.length > 0 ? this.attachmentMetadata() : undefined,
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
    this.clearAttachments();
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
    const fileUploadIds = this.attachmentIds();

    // Must have content or files to submit
    if (!content && fileUploadIds.length === 0) {
      return;
    }

    // Don't submit while uploads are in progress
    if (this.hasActivePendingUploads()) {
      this.toastService.warning('Upload in Progress', 'Please wait for file uploads to complete.');
      return;
    }

    // The empty state's composer is about to be replaced by a compact one (the
    // first send navigates to the new conversation); leave it our height so the
    // replacement shrinks from here instead of popping in.
    if (!this.compact()) {
      this.handoff.leave(this.shell()?.nativeElement.getBoundingClientRect().height ?? 0);
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
    this.clearAttachments();
    this.persistDraftNow();
  }

  cancelChatRequest() {
    this.messageCancelled.emit();
  }

  async startDictation(): Promise<void> {
    if (this.isDictating() || this.isVoiceActive()) return;
    const textarea = this.messageInput()?.nativeElement;
    const text = this.userInput();
    const start = textarea?.selectionStart ?? text.length;
    const end = textarea?.selectionEnd ?? text.length;
    this.dictationAnchor.set({ before: text.slice(0, start), after: text.slice(end) });
    this.ownsDictation.set(true);
    this.settleHints();
    this.closeMentionMenu();
    this.closeSkillMenu();
    // The Dictate button is about to be replaced; keep focus in the composer so
    // Enter (done) and Escape (cancel) work straight from the keyboard.
    textarea?.focus();

    try {
      await this.dictation.start({
        onEnd: (dictated, reason) => this.commitDictation(dictated, reason),
        onError: message => {
          this.releaseDictation();
          this.toastService.error('Dictation', message);
        },
      });
    } catch (err) {
      this.releaseDictation();
      if (err instanceof DictationUnavailableError) {
        this.toastService.info('Dictation', 'Dictation is not available here.');
        return;
      }
      const message = err instanceof Error ? err.message : 'Could not start dictation.';
      this.toastService.error('Dictation', message);
    }
  }

  /** Done: stop listening; the text lands via `commitDictation` once the tail is in. */
  finishDictation(): void {
    if (!this.isDictating()) return;
    this.dictation.finish();
  }

  /** Throw the dictation away and put the composer back exactly as it was. */
  cancelDictation(): void {
    if (!this.ownsDictation()) return;
    this.dictation.cancel();
    this.releaseDictation();
  }

  private commitDictation(dictated: string, reason: DictationEndReason): void {
    const anchor = this.dictationAnchor();
    this.ownsDictation.set(false);
    this.dictationAnchor.set(null);
    if (anchor && dictated.trim()) {
      const { value, caret } = spliceDictation(anchor, dictated);
      this.userInput.set(value);
      const textarea = this.messageInput()?.nativeElement;
      if (textarea) {
        textarea.value = value;
        this.autoResize(textarea);
        textarea.focus();
        textarea.setSelectionRange(caret, caret);
      }
    } else {
      this.restoreComposerAfterDictation();
    }
    if (reason === 'limit') {
      this.toastService.info('Dictation', 'Dictation stopped at its time limit.');
    }
  }

  private releaseDictation(): void {
    this.ownsDictation.set(false);
    this.dictationAnchor.set(null);
    this.restoreComposerAfterDictation();
  }

  /** The textarea was showing the preview; put the user's own text back in it. */
  private restoreComposerAfterDictation(): void {
    const textarea = this.messageInput()?.nativeElement;
    if (!textarea) return;
    textarea.value = this.userInput();
    this.sizeTextareaTo(this.userInput());
    textarea.focus();
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
    // A restored card has no live upload behind it, so it comes off this list
    // instead. Removing from both is safe and saves the caller knowing which
    // kind of card it clicked.
    this.restoredAttachments.update(list =>
      list.filter(attachment => attachment.uploadId !== uploadId),
    );
    this.fileUploadService.clearPendingUpload(uploadId);
  }

  /** Release every attachment on the composer — what a send or a queue clears. */
  private clearAttachments(): void {
    this.restoredAttachments.set([]);
    this.fileUploadService.clearReadyUploads();
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
    this.dropStaleMention(textarea.value);
    this.autoResize(textarea);
    this.syncMentionToken(textarea);
    this.syncSkillToken(textarea);
  }

  /**
   * Keep the mention bound to the `@Name` in the text: once the user deletes or
   * edits the name, the turn goes back to plain chat.
   *
   * The binding is still a separate pick (a hand-typed `@Name` binds nothing),
   * but with no chip beside the input the highlighted name is the only thing
   * saying the turn is handed off — so removing it has to un-hand it. Otherwise
   * a user who deleted the name would send to an Agent with nothing on screen
   * saying so.
   */
  private dropStaleMention(text: string): void {
    const agent = this.mentionedAgent();
    if (agent && !text.includes(`@${agent.name}`)) {
      this.mentionedAgent.set(null);
    }
  }

  /** The mirror behind the textarea has to scroll with it, or the tint slides off its token. */
  onTextareaScroll(event: Event): void {
    const mirror = this.highlightMirror()?.nativeElement;
    if (mirror) mirror.scrollTop = (event.target as HTMLTextAreaElement).scrollTop;
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
    if (this.needsUnfold(textarea)) {
      // Sized again once the wider, stacked layout has rendered.
      this.setUnfolded(true);
      return;
    }
    const height = Math.min(textarea.scrollHeight, MAX_TEXTAREA_HEIGHT_PX);
    textarea.style.height = `${height}px`;
  }

  /** A compact draft that no longer fits on its one line. */
  private needsUnfold(textarea: HTMLTextAreaElement): boolean {
    return (
      this.compact() &&
      !this.unfolded() &&
      textarea.value.length > 0 &&
      (textarea.value.includes('\n') || textarea.scrollHeight > COMPACT_SINGLE_LINE_MAX_PX)
    );
  }

  /**
   * Switch between the one-row and stacked compact layouts, animating the
   * shell's height across the change. The textarea is re-sized after the new
   * layout renders, because its width — and so its wrapping — just changed.
   */
  private setUnfolded(next: boolean): void {
    if (untracked(this.unfolded) === next) return;
    const shell = this.shell()?.nativeElement;
    const from = shell?.getBoundingClientRect().height ?? 0;
    this.unfolded.set(next);
    afterNextRender(
      {
        write: () => {
          const textarea = this.messageInput()?.nativeElement;
          if (textarea) this.sizeTextareaTo(textarea.value);
          if (shell) this.animateShellFrom(shell, from);
        },
      },
      { injector: this.injector },
    );
  }

  /**
   * Ease the shell from `from` to wherever layout has just put it.
   *
   * A Web Animation on `height` rather than a CSS transition: the natural
   * height is `auto`, which does not transition, and the change is a layout
   * swap rather than a property the stylesheet could interpolate. Overflow is
   * clipped only for the length of the animation, so the `@` / `/` menus the
   * shell anchors are never cut off at rest.
   */
  private animateShellFrom(shell: HTMLElement, from: number): void {
    if (this.prefersReducedMotion || typeof shell.animate !== 'function' || from <= 0) return;
    const to = shell.getBoundingClientRect().height;
    if (Math.abs(to - from) < 1) return;
    shell.animate(
      [
        { height: `${from}px`, overflow: 'hidden' },
        { height: `${to}px`, overflow: 'hidden' },
      ],
      { duration: SHELL_RESIZE_MS, easing: 'cubic-bezier(0.2, 0, 0, 1)' },
    );
  }

  /** Collapse the textarea back to a single row (after submit or clear). */
  private resetTextareaHeight(): void {
    const textarea = this.messageInput()?.nativeElement;
    if (!textarea) {
      return;
    }
    textarea.style.height = `${this.compact() ? COMPACT_MIN_TEXTAREA_HEIGHT_PX : MIN_TEXTAREA_HEIGHT_PX}px`;
    textarea.scrollTop = 0;
  }

  onKeyDown(event: KeyboardEvent) {
    // While dictating the textarea is read-only and the keyboard drives the
    // dictation: Enter inserts what was heard, Escape throws it away.
    if (this.isDictating()) {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        this.finishDictation();
      } else if (event.key === 'Escape') {
        event.preventDefault();
        this.cancelDictation();
      }
      return;
    }

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

    // Escape stops a streaming response — only down here, after every menu has
    // had its chance at the key, so closing a menu never also kills the run.
    if (event.key === 'Escape' && this.showStop()) {
      event.preventDefault();
      this.cancelChatRequest();
      return;
    }

    // Submit on Enter (without Shift). On touch, return is the newline key a
    // phone keyboard offers, and sending is the Send button's job.
    if (event.key === 'Enter' && !event.shiftKey && !this.isCoarsePointer()) {
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