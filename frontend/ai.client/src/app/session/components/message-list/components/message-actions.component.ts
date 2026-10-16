import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  PLATFORM_ID,
  signal,
} from '@angular/core';
import { isPlatformBrowser } from '@angular/common';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroSquare2Stack, heroCheck, heroArrowPath, heroHandThumbUp, heroHandThumbDown } from '@ng-icons/heroicons/outline';
import { heroHandThumbUpSolid, heroHandThumbDownSolid } from '@ng-icons/heroicons/solid';
import { MarkdownService } from 'ngx-markdown';
import { FeedbackReason, Message, isTextContentBlock } from '../../../services/models/message.model';
import { FEEDBACK_REASONS, MessageFeedbackService, parseMessageRef } from '../../../services/session/message-feedback.service';
import { TooltipDirective } from '../../../../components/tooltip';
import { ShortcutTargetsService } from '../../../../components/shortcuts/shortcut-targets.service';
import { shortcutAria, withShortcut } from '../../../../components/shortcuts/shortcuts';

@Component({
  selector: 'app-message-actions',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, TooltipDirective],
  providers: [
    provideIcons({
      heroSquare2Stack,
      heroCheck,
      heroArrowPath,
      heroHandThumbUp,
      heroHandThumbDown,
      heroHandThumbUpSolid,
      heroHandThumbDownSolid,
    }),
  ],
  template: `
    <div class="flex items-center gap-1">
      <!-- Copy + thumbs are revealed on hover of the response (the .group
           wrapper in message-list), like the metadata row below them. Opacity
           only — never visibility or display, which would drop these buttons
           out of the tab order and make group-focus-within unreachable, so a
           keyboard user could never get to them. Once a thumb is pressed (or
           a copy just landed) the row stays up, so the state the user set
           doesn't vanish when the pointer leaves. -->
      <div
        class="flex items-center gap-1 transition-opacity duration-200 group-hover:opacity-100 group-focus-within:opacity-100"
        [class.opacity-0]="!actionsPinned()"
      >
        <button
          type="button"
          class="inline-flex items-center justify-center rounded-md p-1.5 text-gray-500 transition-colors hover:bg-gray-200 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
          [appTooltip]="copied() ? 'Copied' : latest() ? copyShortcutTooltip : 'Copy'"
          appTooltipPosition="top"
          [attr.aria-label]="copied() ? 'Copied to clipboard' : 'Copy message'"
          [attr.aria-keyshortcuts]="latest() ? copyShortcutAria : null"
          [disabled]="!hasCopyableText()"
          (click)="copy()"
        >
          @if (copied()) {
            <ng-icon name="heroCheck" class="size-4" aria-hidden="true" />
          } @else {
            <ng-icon name="heroSquare2Stack" class="size-4" aria-hidden="true" />
          }
        </button>

        @if (showFeedback()) {
          <!-- Thumbs: the outcome signal joined to the cost rows. A second click
               on the pressed thumb withdraws it; the other thumb replaces it. -->
          <button
            type="button"
            class="inline-flex items-center justify-center rounded-md p-1.5 transition-colors hover:bg-gray-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-gray-800"
            [class]="thumbClass(1)"
            [appTooltip]="feedbackValue() === 1 ? 'Remove thumbs up' : 'Good response'"
            appTooltipPosition="top"
            [attr.aria-label]="feedbackValue() === 1 ? 'Remove thumbs up' : 'Good response'"
            [attr.aria-pressed]="feedbackValue() === 1"
            [disabled]="feedbackPending()"
            (click)="thumb(1)"
          >
            <ng-icon [name]="feedbackValue() === 1 ? 'heroHandThumbUpSolid' : 'heroHandThumbUp'" class="size-4" aria-hidden="true" />
          </button>
          <button
            type="button"
            class="inline-flex items-center justify-center rounded-md p-1.5 transition-colors hover:bg-gray-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-gray-800"
            [class]="thumbClass(-1)"
            [appTooltip]="feedbackValue() === -1 ? 'Remove thumbs down' : 'Bad response'"
            appTooltipPosition="top"
            [attr.aria-label]="feedbackValue() === -1 ? 'Remove thumbs down' : 'Bad response'"
            [attr.aria-pressed]="feedbackValue() === -1"
            [disabled]="feedbackPending()"
            (click)="thumb(-1)"
          >
            <ng-icon [name]="feedbackValue() === -1 ? 'heroHandThumbDownSolid' : 'heroHandThumbDown'" class="size-4" aria-hidden="true" />
          </button>

          @if (feedbackValue() === -1) {
            <!-- Reason codes only — a closed set, no text field, by design. -->
            <div class="flex flex-wrap items-center gap-1 pl-1" role="group" aria-label="Why was this response bad?">
              @for (reason of reasons; track reason) {
                <button
                  type="button"
                  class="rounded-2xl border px-2 py-0.5 text-xs/5 font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                  [class]="reasonClass(reason)"
                  [attr.aria-pressed]="feedbackReason() === reason"
                  [disabled]="feedbackPending()"
                  (click)="pickReason(reason)"
                >
                  {{ reasonLabels[reason] }}
                </button>
              }
              <button
                type="button"
                class="ml-1 inline-flex items-center gap-1 rounded-2xl px-2 py-0.5 text-xs/5 font-medium text-primary-accessible transition-colors hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark dark:hover:bg-gray-700"
                appTooltip="Prefill a correction you can edit, then send"
                appTooltipPosition="top"
                aria-label="Retry with that in mind"
                [disabled]="feedbackPending()"
                (click)="retry()"
              >
                <ng-icon name="heroArrowPath" class="size-3.5" aria-hidden="true" />
                <span>Retry with that in mind</span>
              </button>
            </div>
          }
        }
      </div>

      <!-- Continue / interrupted chips are NOT hover-gated: they report that
           the answer is incomplete and offer the only way to finish it, which
           a user has to be able to see without knowing to hover. -->
      @if (canContinue()) {
        <span class="pl-1 text-xs text-gray-500 dark:text-gray-400">
          Response length limit reached
        </span>
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-sm font-medium text-primary-accessible transition-colors hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark dark:hover:bg-gray-700 dark:hover:text-primary-50"
          appTooltip="Resume response"
          appTooltipPosition="top"
          aria-label="Continue the truncated response"
          (click)="onContinue()"
        >
          <ng-icon name="heroArrowPath" class="size-4" aria-hidden="true" />
          <span>Continue</span>
        </button>
      } @else if (interruptedReason() === 'connection_lost') {
        <span class="pl-1 text-xs text-gray-500 dark:text-gray-400">
          Response interrupted
        </span>
        <button
          type="button"
          class="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-sm font-medium text-primary-accessible transition-colors hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark dark:hover:bg-gray-700 dark:hover:text-primary-50"
          appTooltip="Resume response"
          appTooltipPosition="top"
          aria-label="Continue the interrupted response"
          (click)="onContinue()"
        >
          <ng-icon name="heroArrowPath" class="size-4" aria-hidden="true" />
          <span>Continue</span>
        </button>
      } @else if (interruptedReason() === 'user_stopped') {
        <span class="pl-1 text-xs text-gray-500 dark:text-gray-400">
          You stopped this response
        </span>
      }
    </div>
  `,
  styles: `
    @reference "../../../../../styles/theme.css";

    :host {
      display: contents;
    }

    button[disabled] {
      opacity: 0.5;
      cursor: not-allowed;
    }
  `,
})
export class MessageActionsComponent {
  private platformId = inject(PLATFORM_ID);
  private isBrowser = isPlatformBrowser(this.platformId);
  private markdown = inject(MarkdownService);
  private feedbackService = inject(MessageFeedbackService);
  private shortcutTargets = inject(ShortcutTargetsService);

  protected readonly copyShortcutTooltip = withShortcut('Copy', 'copyLastResponse');
  protected readonly copyShortcutAria = shortcutAria('copyLastResponse');

  /**
   * The assistant messages of one run (see AssistantMessageComponent).
   *
   * A run rather than a message because the agent loop emits a separate
   * Bedrock message per tool round trip, and Copy must yield the whole
   * response — text the model wrote before a tool call is still part of what
   * the user is reading. Taking only the final message would silently drop it.
   */
  messages = input.required<Message[]>();

  /** The newest response in the conversation: Cmd/Ctrl+Shift+C copies this one. */
  latest = input<boolean>(false);

  constructor() {
    effect(onCleanup => {
      if (!this.latest()) return;
      onCleanup(
        this.shortcutTargets.registerLatestResponse({
          canCopy: () => this.isBrowser && this.hasCopyableText(),
          copy: () => this.copy(),
        }),
      );
    });
  }

  /** Show a "Continue" button when this is the last assistant message of a
   *  recoverable max_tokens-truncated turn. */
  canContinue = input<boolean>(false);

  /** When set on the last assistant message of an interrupted turn, shows a
   *  "Response interrupted" chip (+ Continue for 'connection_lost') or a
   *  "You stopped this response" chip (for 'user_stopped'). */
  interruptedReason = input<'user_stopped' | 'connection_lost' | null>(null);

  /** Emitted when the user asks to continue the truncated / interrupted
   *  response. */
  continueRequested = output<void>();

  /** Continue click: emit as before, and record the implicit signal. */
  protected onContinue(): void {
    const last = this.lastMessage();
    if (last) this.feedbackService.recordSignal(last, 'continue');
    this.continueRequested.emit();
  }

  protected copied = signal(false);
  private resetTimeout: ReturnType<typeof setTimeout> | null = null;

  protected copyableText = computed(() =>
    this.messages()
      .flatMap((message) => message.content.filter(isTextContentBlock))
      .map((block) => block.text)
      .join('\n\n')
      .trim(),
  );

  protected hasCopyableText = computed(() => this.copyableText().length > 0);

  /** Keep the copy/thumbs row visible without a hover when it carries state
   *  the user set: a cast thumb (and its reason chips) or a just-copied
   *  confirmation. Otherwise it fades in on hover/focus of the response. */
  protected actionsPinned = computed(() => this.copied() || this.feedbackValue() !== null);

  // ── feedback ──
  // The thumb keys on the run's LAST message (the one whose cost row
  // describes the finished answer — see message-list.component.html).
  protected readonly reasons = FEEDBACK_REASONS;
  protected readonly reasonLabels: Record<FeedbackReason, string> = {
    wrong: 'Wrong or made up',
    instructions: 'Ignored instructions',
    length: 'Too long / short',
    tool_failed: 'A tool failed',
    outdated: 'Out of date',
    other: 'Something else',
  };

  private lastMessage = computed<Message | null>(() => {
    const messages = this.messages();
    return messages.length > 0 ? messages[messages.length - 1] : null;
  });

  /** Only messages with a server-shaped id (`msg-{session}-{index}`) can be
   *  thumbed: that index is the key the cost row shares. */
  protected showFeedback = computed(() => {
    const last = this.lastMessage();
    return !!last && !this.feedbackService.unavailable() && parseMessageRef(last.id) !== null;
  });

  protected feedback = computed(() => {
    const last = this.lastMessage();
    return last ? this.feedbackService.feedbackFor(last) : null;
  });
  protected feedbackValue = computed(() => this.feedback()?.value ?? null);
  protected feedbackReason = computed(() => this.feedback()?.reason ?? null);
  protected feedbackPending = computed(() => {
    const last = this.lastMessage();
    return !!last && this.feedbackService.isPending()(last.id);
  });

  thumb(value: 1 | -1): void {
    const last = this.lastMessage();
    if (!last) return;
    if (this.feedbackValue() === value) {
      void this.feedbackService.clearFeedback(last);
    } else {
      void this.feedbackService.setFeedback(last, value);
    }
  }

  /** The consequence: draft a correction for this thumb into the composer. */
  retry(): void {
    const last = this.lastMessage();
    if (!last || this.feedbackValue() !== -1) return;
    this.feedbackService.requestRetry(last);
  }

  pickReason(reason: FeedbackReason): void {
    const last = this.lastMessage();
    if (!last || this.feedbackValue() !== -1) return;
    void this.feedbackService.setFeedback(last, -1, reason);
  }

  protected thumbClass(value: 1 | -1): string {
    return this.feedbackValue() === value
      ? 'text-primary-accessible dark:text-primary-accessible-dark'
      : 'text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200';
  }

  protected reasonClass(reason: FeedbackReason): string {
    return this.feedbackReason() === reason
      ? 'border-primary-accessible bg-primary-accessible text-white dark:border-primary-accessible-dark dark:bg-primary-accessible-dark dark:text-gray-900'
      : 'border-gray-300 bg-white text-gray-600 hover:bg-gray-100 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700';
  }

  /** Resolves whether anything reached the clipboard. */
  async copy(): Promise<boolean> {
    if (!this.isBrowser || !this.hasCopyableText()) return false;

    const markdown = this.copyableText();

    try {
      await this.writeRichClipboard(markdown);
      this.copied.set(true);
      // Implicit signal (spec §10): a copied answer was worth keeping.
      const last = this.lastMessage();
      if (last) this.feedbackService.recordSignal(last, 'copy');

      if (this.resetTimeout) {
        clearTimeout(this.resetTimeout);
      }
      this.resetTimeout = setTimeout(() => {
        this.copied.set(false);
        this.resetTimeout = null;
      }, 2000);
      return true;
    } catch {
      // Clipboard API may be unavailable (insecure context, permissions). No-op.
      return false;
    }
  }

  /**
   * Write both rendered HTML and raw markdown to the clipboard so rich-text
   * targets (Google Docs, Word, Gmail) paste formatted content while plain-
   * text targets (terminals, code editors) still get the markdown source.
   * Falls back to plain-text only when ClipboardItem isn't available.
   */
  private async writeRichClipboard(markdown: string): Promise<void> {
    const html = await Promise.resolve(this.markdown.parse(markdown));
    const fullHtml = `<meta charset="utf-8">${html}`;

    if (typeof ClipboardItem !== 'undefined' && navigator.clipboard.write) {
      const item = new ClipboardItem({
        'text/html': new Blob([fullHtml], { type: 'text/html' }),
        'text/plain': new Blob([markdown], { type: 'text/plain' }),
      });
      await navigator.clipboard.write([item]);
      return;
    }

    await navigator.clipboard.writeText(markdown);
  }
}
