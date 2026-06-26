import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  signal,
  viewChild,
} from '@angular/core';
import { DOCUMENT } from '@angular/common';
import type {
  ToolResultData,
  ToolResultRenderer,
} from '../tool-renderer-registry.service';
import { McpAppStateService } from '../../../../../services/mcp-apps/mcp-app-state.service';
import { StreamParserService } from '../../../../../services/chat/stream-parser.service';
import { ThemeService } from '../../../../../../components/topnav/components/theme-toggle/theme.service';
import { McpAppBridge } from '../../../../../services/mcp-apps/mcp-app-bridge';
import { McpAppProxyService } from '../../../../../services/mcp-apps/mcp-app-proxy.service';
import { McpAppMessageService } from '../../../../../services/mcp-apps/mcp-app-message.service';
import { McpAppConsentService } from '../../../../../services/mcp-apps/mcp-app-consent.service';
import { buildProxyUrl } from '../../../../../services/mcp-apps/proxy-url';
import { McpAppConsentPromptComponent } from '../../mcp-app-consent-prompt/mcp-app-consent-prompt.component';
import { JsonSyntaxHighlightPipe } from '../json-syntax-highlight.pipe';
import type { DisplayMode } from '../../../../../services/mcp-apps/mcp-app-protocol';
import { ChatRequestService } from '../../../../../services/chat/chat-request.service';
import { ChatStateService } from '../../../../../services/chat/chat-state.service';
import { SessionService } from '../../../../../services/session/session.service';

/**
 * MCP App renderer (SEP-1865), PR #4 of
 * `docs/kaizen/scoping/mcp-apps-host-renderer.md`.
 *
 * Resolves to this component (instead of the default text/JSON renderer)
 * when the tool invocation produced a `ui_resource` event — see the
 * `resultRenderer` computed in `ToolUseComponent`. Renders the outer
 * sandbox-proxy iframe at the deployed `sandboxOrigin` and drives the host
 * half of the postMessage bridge; the proxy loads the actual App HTML in
 * its inner null-origin iframe with a per-resource CSP.
 *
 * The whole surface is dark until the backend host flag is flipped (PR #7),
 * so in practice no `ui_resource` arrives and the registry never resolves
 * here. When it has no resource for its `toolUseId` (e.g. after a reload —
 * the inline event doesn't re-hydrate) it renders nothing and the tool-use
 * card falls back to the default renderer path.
 */
@Component({
  selector: 'app-mcp-app-frame',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [McpAppConsentPromptComponent, JsonSyntaxHighlightPipe],
  styles: `
    :host {
      display: block;
    }

    /*
     * Tool-name shimmer while the App's tool is still running (SEP-1865
     * Claude parity). A moving highlight masked to the text via
     * background-clip:text. The gradient uses EXPLICIT gray tones, NOT
     * currentColor — because this rule also sets the text fill transparent so
     * the gradient shows through, and a currentColor gradient would resolve to
     * transparent (= invisible text). Light + dark handled via :host-context.
     */
    .tool-name-shimmer {
      background-image: linear-gradient(
        90deg,
        #4b5563 25%,
        #d1d5db 50%,
        #4b5563 75%
      );
      background-size: 200% 100%;
      background-clip: text;
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      color: transparent;
      animation: tool-name-shimmer 1.4s linear infinite;
    }

    :host-context(.dark) .tool-name-shimmer {
      background-image: linear-gradient(
        90deg,
        #d1d5db 25%,
        #f3f4f6 50%,
        #d1d5db 75%
      );
    }

    @keyframes tool-name-shimmer {
      0% {
        background-position: 200% 0;
      }
      100% {
        background-position: -200% 0;
      }
    }

    @media (prefers-reduced-motion: reduce) {
      .tool-name-shimmer {
        animation: none;
        background-image: none;
        -webkit-text-fill-color: #4b5563;
        color: #4b5563;
      }

      :host-context(.dark) .tool-name-shimmer {
        -webkit-text-fill-color: #d1d5db;
        color: #d1d5db;
      }
    }

    /* Loading skeleton shown while the App HTML is fetched (header is already
       up). A slow sweep over a faint surface, theme-derived via currentColor. */
    .mcp-app-skeleton {
      background: linear-gradient(
        100deg,
        color-mix(in srgb, currentColor 4%, transparent) 30%,
        color-mix(in srgb, currentColor 9%, transparent) 50%,
        color-mix(in srgb, currentColor 4%, transparent) 70%
      );
      color: rgb(107 114 128); /* gray-500 — only feeds the color-mix above */
      background-size: 200% 100%;
      animation: mcp-app-skeleton 1.6s ease-in-out infinite;
    }

    @keyframes mcp-app-skeleton {
      0% {
        background-position: 200% 0;
      }
      100% {
        background-position: -200% 0;
      }
    }

    @media (prefers-reduced-motion: reduce) {
      .mcp-app-skeleton {
        animation: none;
      }
    }
  `,
  template: `
    @if (canRenderApp()) {
      <div
        [class]="containerClasses()"
        [attr.role]="displayMode() === 'fullscreen' ? 'dialog' : null"
        [attr.aria-modal]="displayMode() === 'fullscreen' ? 'true' : null"
        [attr.aria-label]="displayMode() === 'fullscreen' ? 'MCP App, fullscreen' : null"
        (keydown.escape)="exitFullscreen()"
      >
        <!--
          Connected header — the App's provenance (icon + server + tool +
          status), styled as the iframe's title bar. Shown in BOTH modes: in
          fullscreen it doubles as the overlay's title bar and hosts the Exit
          control, so the exit affordance can't overlap the App's own
          top-corner chrome (e.g. Excalidraw's toolbar). Inline, the trailing
          slot instead hosts the request/response details toggle.
        -->
        <div
          class="flex shrink-0 items-center gap-2 border-b border-gray-200 bg-gray-50 px-3 py-2 dark:border-gray-700 dark:bg-gray-800/60"
        >
          @if (icon() && !iconFailed()) {
            <img
              [src]="icon()"
              [alt]="serverName() + ' icon'"
              class="size-4 shrink-0 rounded-sm object-contain"
              (error)="iconFailed.set(true)"
            />
          } @else {
            <!-- Generic MCP/app glyph fallback -->
            <svg
              class="size-4 shrink-0 text-gray-400 dark:text-gray-500"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              aria-hidden="true"
            >
              <path
                stroke-linecap="round"
                stroke-linejoin="round"
                d="M4 7l8-4 8 4v10l-8 4-8-4V7zm8-4v18m8-14l-8 4-8-4"
              />
            </svg>
          }

          @if (serverName()) {
            <span
              class="shrink-0 text-sm font-medium text-gray-700 dark:text-gray-200"
              >{{ serverName() }}</span
            >
            <span class="shrink-0 text-gray-400 dark:text-gray-500" aria-hidden="true"
              >·</span
            >
          }

          <span
            class="min-w-0 truncate font-mono text-sm text-gray-600 dark:text-gray-300"
            [class.tool-name-shimmer]="isRunning()"
            >{{ displayToolName() }}</span
          >

          @if (isError()) {
            <span
              class="shrink-0 text-xs font-medium text-red-600 dark:text-red-400"
              >Failed</span
            >
          }

          @if (displayMode() === 'fullscreen') {
            <button
              type="button"
              class="ml-auto flex shrink-0 items-center gap-1.5 rounded-md border border-gray-300 bg-white px-2.5 py-1 text-sm font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
              (click)="exitFullscreen()"
            >
              <svg
                class="size-4"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                aria-hidden="true"
              >
                <path
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  d="M9 9V4.5M9 9H4.5M9 9 3.75 3.75M9 15v4.5M9 15H4.5M9 15l-5.25 5.25M15 9h4.5M15 9V4.5M15 9l5.25-5.25M15 15h4.5M15 15v4.5m0-4.5 5.25 5.25"
                />
              </svg>
              Exit fullscreen
            </button>
          } @else {
            <button
              type="button"
              class="ml-auto shrink-0 rounded-sm p-1 text-gray-500 hover:bg-gray-200 hover:text-gray-600 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-blue-500 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-300"
              [attr.aria-expanded]="detailsExpanded()"
              [attr.aria-label]="
                detailsExpanded() ? 'Hide request and response' : 'Show request and response'
              "
              (click)="toggleDetails()"
            >
              <svg
                class="size-4"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                aria-hidden="true"
              >
                <path
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  d="M8 9l-4 3 4 3m8-6l4 3-4 3"
                />
              </svg>
            </button>
          }
        </div>

        @if (displayMode() !== 'fullscreen' && detailsExpanded()) {
          <div
            class="space-y-2 border-b border-gray-200 bg-gray-50 px-3 py-2 dark:border-gray-700 dark:bg-gray-800/60"
          >
            <div>
              <div class="mb-1 text-xs font-medium text-gray-700 dark:text-gray-300">
                Request
              </div>
              <pre
                class="overflow-x-auto rounded-sm border border-gray-300 bg-gray-100 p-2 font-mono text-xs dark:border-gray-600 dark:bg-gray-900"
              ><code [innerHTML]="requestJson() | jsonSyntaxHighlight"></code></pre>
            </div>
            @if (responseText()) {
              <div>
                <div
                  class="mb-1 flex items-center gap-1.5 text-xs font-medium text-gray-700 dark:text-gray-300"
                >
                  <span>Response</span>
                  @if (isError()) {
                    <span class="text-red-600 dark:text-red-400">(Error)</span>
                  }
                </div>
                <pre
                  class="overflow-x-auto rounded-sm border border-gray-300 bg-gray-100 p-2 font-mono text-xs dark:border-gray-600 dark:bg-gray-900"
                ><code [innerHTML]="responseText() | jsonSyntaxHighlight"></code></pre>
              </div>
            }
          </div>
        }

        <!--
          App-initiated consent (e.g. ui/open-link from "Open in
          Excalidraw"). Rendered INSIDE the frame, below the title bar and
          above the iframe, so it lives within the fullscreen overlay — a
          prompt left outside the overlay is painted over by it (z-[9999]),
          so the user can never grant it and the App's request hangs (the
          export button stuck on "exporting…"). shrink-0 keeps it from being
          squeezed in the fullscreen flex column.
        -->
        @if (currentPrompt(); as prompt) {
          <div class="shrink-0 px-3 py-2">
            <app-mcp-app-consent-prompt [prompt]="prompt" />
          </div>
        }

        @if (proxyUrl(); as url) {
          <div #host [class]="hostClasses()"></div>
        } @else {
          <!-- App HTML still loading (between the header shell and the full
               ui_resource). A skeleton sized to the frame so the card doesn't
               jump when the iframe mounts. Hidden in fullscreen. -->
          @if (displayMode() !== 'fullscreen') {
            <div
              class="flex items-center justify-center bg-white dark:bg-gray-900"
              [style.height.px]="frameHeight()"
              aria-hidden="true"
            >
              <div class="mcp-app-skeleton h-full w-full"></div>
            </div>
          }
        }
      </div>
    }
  `,
})
export class McpAppFrameComponent implements ToolResultRenderer {
  /** Tool result payload (the renderer contract). Mapped to CallToolResult. */
  readonly result = input.required<ToolResultData>();
  readonly minimized = input<boolean>(false);
  /** Originating tool-use id — keys the resource + correlates tool data. */
  readonly toolUseId = input<string>();

  /**
   * Whether the tool's argument streaming has finished — bound by the host to
   * the arrival of the REAL tool result (`!!toolUse.result`). This is the
   * authoritative "input is final" signal for an early-mounted frame.
   *
   * It exists because `result` is a non-null *success stub*
   * (`{content: [], status: 'success'}`) from the moment the frame mounts at
   * the tool's `content_block_start` — the host passes the stub so the
   * required `result` input is satisfied while the tool is still running. That
   * makes `result() != null` true from the very first frame, so it CANNOT
   * distinguish "arguments still streaming" from "tool done". Keying finality
   * on this flag instead lets the partial-tool-input stream actually relay
   * while the model generates the arguments (the progressive camera tour) and
   * defers the complete `tool-input` until the input is genuinely final —
   * preventing a premature empty final from latching `toolInputSent` and
   * leaving the App with `{}` (the blank canvas). The live stream parser does
   * not retain an MCP tool's parsed input by relay time, so `lookupToolInput()`
   * can't serve as this signal either.
   */
  readonly inputComplete = input<boolean>(false);

  /**
   * The tool's persisted arguments, from `GET /messages` on reload. The live
   * path resolves the input from the stream parser / captured partial, but
   * after a refresh both are empty (the live stream isn't replayed); this is
   * the fallback that lets the App render its final state instead of a blank
   * canvas. On the live path it's `{}` until the tool-use block finalizes (the
   * parser leaves an in-flight block's input empty), so it never pre-empts the
   * streaming tour. No tour is replayed on reload — the App snaps to the
   * complete input, the spec's `tool-input` (final) path.
   */
  readonly toolInput = input<Record<string, unknown>>({});

  /**
   * The agent-facing tool name (e.g. `create_view`), shown in the App header
   * next to the server identity. Bound by the host from the tool-use block;
   * the server name + icon come from the `ui_resource` instead (see
   * `serverName`/`icon`).
   */
  readonly toolName = input<string>('');

  private readonly mcpAppState = inject(McpAppStateService);
  private readonly mcpAppProxy = inject(McpAppProxyService);
  private readonly mcpAppMessage = inject(McpAppMessageService);
  private readonly mcpAppConsent = inject(McpAppConsentService);
  private readonly chatRequest = inject(ChatRequestService);
  private readonly chatState = inject(ChatStateService);
  private readonly conversation = inject(SessionService);
  private readonly streamParser = inject(StreamParserService);
  private readonly theme = inject(ThemeService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly doc = inject(DOCUMENT);
  private readonly win = this.doc.defaultView;

  private readonly hostRef =
    viewChild<ElementRef<HTMLDivElement>>('host');
  private iframeEl: HTMLIFrameElement | null = null;
  /** Prior `body.overflow` saved while this frame holds the fullscreen lock
   *  (null ⇒ this frame isn't locking — never restore what we didn't set). */
  private lockedBodyOverflow: string | null = null;

  /** Initial height; the App drives it via `ui/notifications/size-changed`. */
  protected readonly frameHeight = signal(360);

  /**
   * Current display mode. The App requests changes via
   * `ui/request-display-mode` (routed through the bridge); the user can
   * leave fullscreen via the Exit button or Escape. In `fullscreen` the
   * CONTAINER becomes a fixed full-viewport flex column (title bar + iframe)
   * and the iframe absolute-fills its flex-1 host below the header (see the
   * style effect) — keeping the header visible so the Exit control lives in a
   * title bar instead of a floating chip that overlaps the App's own
   * top-corner chrome. Toggling mode only changes CSS (no DOM move), so the
   * running App never reloads and keeps its state.
   */
  protected readonly displayMode = signal<DisplayMode>('inline');

  /**
   * Outer wrapper classes. In fullscreen the wrapper IS the overlay: a fixed
   * full-viewport flex column holding the title bar (the connected header)
   * above the iframe's flex-1 host. Inline it's the bordered, rounded card.
   * Both carry `dark:bg-gray-900` so the header's translucent dark fill
   * composites over the same surface in either mode — an inline card left
   * `bg-white` in dark mode turned the header a muddy mid-grey and dropped
   * the tool name to ~1.5:1 (grey-on-grey).
   */
  protected readonly containerClasses = computed(() =>
    this.displayMode() === 'fullscreen'
      ? 'fixed inset-0 z-[9999] flex flex-col bg-white dark:bg-gray-900'
      : 'relative overflow-hidden rounded-sm border border-gray-300 bg-white dark:border-gray-600 dark:bg-gray-900',
  );

  /**
   * Classes for the iframe's host div. In fullscreen it's the flex-1 region
   * below the title bar that the iframe absolute-fills; inline it's a plain
   * block sized by the iframe's own height.
   */
  protected readonly hostClasses = computed(() =>
    this.displayMode() === 'fullscreen'
      ? 'relative min-h-0 flex-1 overflow-hidden'
      : 'block',
  );

  private bridge: McpAppBridge | null = null;
  private readonly nonce =
    this.win?.crypto?.randomUUID?.() ?? `n-${Math.random().toString(36).slice(2)}`;

  /** The UI resource for this tool invocation (undefined ⇒ render nothing). */
  protected readonly resource = computed(() => {
    const id = this.toolUseId();
    return id ? this.mcpAppState.get(id) : undefined;
  });

  /** Whether the header's server icon `<img>` failed to load (→ glyph). */
  protected readonly iconFailed = signal(false);

  /** Whether the request/response details strip is expanded (the `</>` toggle). */
  protected readonly detailsExpanded = signal(false);

  /**
   * Server display name for the header. Prefers the backend-resolved
   * `serverName` on the resource (serverInfo title/name → `ui://` authority),
   * with a client-side authority parse as a last resort for resources
   * persisted before that field shipped.
   */
  protected readonly serverName = computed(() => {
    const res = this.resource();
    if (res?.serverName) return res.serverName;
    return this.serverNameFromUri(res?.resourceUri ?? '');
  });

  /** Server icon `src` from the resource, or "" (→ glyph fallback). */
  protected readonly icon = computed(() => this.resource()?.icon ?? '');

  /**
   * Tool name for the header. Prefers the name carried on the `ui_resource`
   * (recorded atomically with the frame's promotion, so the name + shimmer
   * appear immediately), falling back to the `toolName` input from the streamed
   * message content (the reload path, and resources persisted before the event
   * carried it).
   */
  protected readonly displayToolName = computed(
    () => this.resource()?.toolName || this.toolName(),
  );

  /** The tool produced an error result (drives the header's "Failed" badge). */
  protected readonly isError = computed(
    () => this.result()?.status === 'error',
  );

  /**
   * Whether the tool is still running — drives the tool-name shimmer. True
   * from mount until the real result lands (`inputComplete`), covering both
   * the argument-streaming and tool-execution windows. Never shimmers a
   * failed call.
   */
  protected readonly isRunning = computed(
    () => !this.inputComplete() && !this.isError(),
  );

  /** Pretty-printed request arguments for the expanded details strip. */
  protected readonly requestJson = computed(() =>
    JSON.stringify(this.resolvedToolInput(), null, 2),
  );

  /** Tool response rendered as text/JSON for the expanded details strip. */
  protected readonly responseText = computed(() => {
    const content = this.result()?.content ?? [];
    const parts = content.map((item) => {
      if (item.json !== undefined) return JSON.stringify(item.json, null, 2);
      if (item.text) return item.text;
      if (item.image) return `[image/${item.image.format}]`;
      return '';
    });
    return parts.filter(Boolean).join('\n');
  });

  protected toggleDetails(): void {
    this.detailsExpanded.update((v) => !v);
  }

  /**
   * Title-case a `ui://<authority>/…` authority as a server-name fallback
   * (`ui://excalidraw/canvas` → "Excalidraw"), mirroring the backend's
   * `_server_name_from_uri`. Used only when the resource carries no
   * `serverName`.
   */
  private serverNameFromUri(resourceUri: string): string {
    const match = /^[a-z][a-z0-9+.-]*:\/\/([^/]+)/i.exec(resourceUri);
    const authority = match?.[1] ?? '';
    return authority
      .split(/[-_.\s]+/)
      .filter(Boolean)
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
      .join(' ');
  }

  /**
   * Latest server-healed streamed partial tool input for this invocation, or
   * undefined. Updated repeatedly while the tool's arguments stream (after the
   * frame mounts early); relayed to the App for progressive rendering.
   */
  protected readonly partialInput = computed(() => {
    const id = this.toolUseId();
    return id ? this.mcpAppState.getPartialInput(id) : undefined;
  });

  /**
   * Whether the tool's input has finished streaming. The stream parser leaves
   * an in-flight tool-use block's `input` as `{}` (its accumulating JSON can't
   * parse yet) and fills the parsed object only once the block finalizes at
   * `content_block_stop` — so a non-empty `lookupToolInput()` is a precise
   * "arguments fully streamed" signal that fires BEFORE the tool executes.
   * Keying on it (rather than the later `tool_result`) lets the App receive
   * the complete `tool-input` — and render every element, including the last —
   * as soon as streaming ends, instead of lagging until the result lands.
   * `inputComplete` (the real tool result having landed) is the fallback for
   * empty-input tools and the reload path, where the live stream isn't
   * replayed. Until final, the App gets `tool-input-partial`.
   *
   * NB: this keys on `inputComplete()`, NOT `result() != null` — the latter is
   * true from first mount because the host passes a non-null success stub for
   * the still-pending tool, which would force this true immediately, suppress
   * the partial relay, and fire an empty final. See `inputComplete`.
   */
  protected readonly inputFinal = computed(
    () =>
      Object.keys(this.lookupToolInput()).length > 0 ||
      this.inputComplete() ||
      // Reload: a non-empty persisted input is itself a "final" signal, so an
      // interrupted tool (input persisted, no result → `inputComplete` false)
      // still renders instead of waiting on a result that never lands.
      Object.keys(this.toolInput()).length > 0,
  );

  /**
   * The complete tool arguments to deliver as the final `tool-input`. Prefers
   * the stream parser's parsed input, but falls back to the last server-healed
   * streamed partial when the parser has none — which is the common case for
   * an MCP App frame: the parser's live `allMessages()` no longer carries this
   * tool's parsed input by the time the frame relays it (turn finished, or the
   * mount was deferred behind a capability-consent prompt). The accumulated
   * partial IS the complete input once streaming ends, so this is what lets
   * the App actually render its elements instead of receiving `{}` (the
   * long-standing "blank canvas"). On reload both the live parser and the
   * captured partial are empty (the stream isn't replayed), so it falls back
   * to `toolInput()` — the arguments persisted with the message. Used only for
   * the FINAL send; `inputFinal` stays keyed on stream completion so partials
   * still drive the live tour.
   */
  private resolvedToolInput(): Record<string, unknown> {
    const live = this.lookupToolInput();
    if (Object.keys(live).length > 0) return live;
    const partial = this.partialInput();
    if (partial) return partial;
    // Reload fallback: the live parser and captured partial are both gone
    // after a refresh, so use the arguments persisted with the message.
    return this.toolInput();
  }

  /** Id of this frame's currently-open consent prompt (`ui/open-link`),
   *  rendered inside the frame (below the title bar, above the iframe) so it
   *  stays anchored to the App and visible in fullscreen — where the frame is
   *  a fixed overlay and a prompt rendered outside it would be occluded,
   *  leaving the App's request unanswerable. */
  private readonly openPromptId = signal<string | null>(null);
  protected readonly currentPrompt = computed(() => {
    const id = this.openPromptId();
    if (!id) return null;
    return this.mcpAppConsent.pending().find((p) => p.id === id) ?? null;
  });

  /**
   * Permissions applied to the frame — the App's declared
   * `_meta.ui.permissions` mapped straight onto the iframe `allow`
   * (Permissions-Policy), matching the SEP-1865 reference host (and Claude).
   * We deliberately do NOT pre-prompt for capabilities: delegating a feature
   * via `allow` does not *activate* it — the BROWSER prompts at use-time for
   * camera/microphone/geolocation, and clipboard-write is low-risk and needs
   * no prompt. An earlier PR #6 gate held the mount behind an inline consent
   * prompt; that was stricter than the reference, confused users (Claude
   * shows no such prompt), and — fatally — delayed the iframe mount past the
   * argument-streaming window, breaking progressive rendering (the camera
   * tour). `ui/open-link` still routes through `McpAppConsentService`.
   */
  private readonly effectivePermissions = computed(
    () => this.resource()?.permissions ?? {},
  );

  /**
   * Plain string URL the iframe `src` is set to. Null until the resource +
   * sandbox origin are known; the App is then framed immediately (no consent
   * gate) so the bridge is live while arguments stream. Trusted single value
   * from our authenticated backend (SSM-sourced); the imperative sandbox
   * attribute + the proxy's per-resource CSP are the real containment, same
   * justification as the artifact panel.
   *
   * The `?csp=` query the proxy CFN reads is built from the resource's
   * declared `_meta.ui.csp` (`buildProxyUrl`). Apps that declare nothing
   * get the bare URL and the proxy's default CSP — no cache fragmentation
   * for the no-declaration majority.
   */
  protected readonly proxyUrl = computed<string | null>(() => {
    const res = this.resource();
    // Gate the iframe mount on a non-empty `html`: the App frame is promoted
    // (and its header shown) the instant the backend emits the header-only
    // `ui_resource` shell at the tool's `content_block_start` — but that shell
    // carries `html: ''` (the real HTML follows after `resources/read`). Wait
    // for it so the iframe mounts ONCE with the full App, not an empty shell.
    if (!res || !res.sandboxOrigin || !res.html) return null;
    return buildProxyUrl(res.sandboxOrigin, res.csp);
  });

  /**
   * Whether to render the App card at all — true once a resource with a
   * sandbox origin exists (the header-only shell qualifies). An empty
   * `sandboxOrigin` means the mcp-sandbox stack isn't wired, so the SPA can't
   * frame the App: render nothing and let the tool fall back to a plain card.
   */
  protected readonly canRenderApp = computed(
    () => !!this.resource()?.sandboxOrigin,
  );

  /** Permissions-Policy `allow` for the outer frame (delegates to inner). */
  protected readonly allowAttr = computed(() => {
    const p = this.effectivePermissions();
    const feats: string[] = [];
    if (p.camera) feats.push('camera');
    if (p.microphone) feats.push('microphone');
    if (p.geolocation) feats.push('geolocation');
    if (p.clipboardWrite) feats.push('clipboard-write');
    return feats.length ? feats.join('; ') : null;
  });

  constructor() {
    // Push theme changes to the App as a host-context-changed partial.
    effect(() => {
      const theme = this.theme.theme();
      this.bridge?.notifyHostContextChanged({ theme });
    });
    // Relay each streamed partial tool input to the App as it arrives, so a
    // progressively-rendering App (e.g. Excalidraw's guided camera tour)
    // animates in lockstep with the model generating the arguments. The
    // backend streams `ui_tool_input_partial` in true real time (Bedrock's
    // fine-grained tool streaming — see `model_config.to_bedrock_config`), so
    // no host-side pacing is needed; each partial is forwarded straight
    // through. Gated on `viewIsInitialized`: partials that arrive before the
    // bridge handshake completes are skipped here and instead caught up in one
    // shot by the init seed (`getPartialToolInput` below), so they don't pile
    // into `preInitQueue` and flush as a burst. Stops once the input is final
    // (the complete `tool-input` is sent by the effect below).
    effect(() => {
      const partial = this.partialInput();
      if (!partial || this.inputFinal()) return;
      if (!this.bridge?.viewIsInitialized) return;
      this.bridge.sendToolInputPartial(partial);
    });
    // On finality, send the complete `tool-input` (once) then (re-)push the
    // tool result if it's landed/changed after the App initialized.
    effect(() => {
      this.result();
      if (this.inputFinal()) this.bridge?.sendToolInputFinal();
      this.bridge?.refreshToolResult();
    });
    // Imperatively create the iframe once the host div mounts. Angular 21
    // forbids dynamic `[attr.allow]` on <iframe> (NG0910), so we build the
    // element by hand with all attributes set before src — the browser only
    // consults `allow` at load-start.
    effect(() => {
      const host = this.hostRef();
      const url = this.proxyUrl();
      if (!host || !url) {
        if (this.iframeEl) {
          this.iframeEl.remove();
          this.iframeEl = null;
        }
        return;
      }
      if (this.iframeEl) return;
      const iframe = this.doc.createElement('iframe');
      iframe.setAttribute('title', 'MCP App');
      iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin');
      iframe.setAttribute('referrerpolicy', 'no-referrer');
      iframe.setAttribute('loading', 'lazy');
      const allow = this.allowAttr();
      if (allow) iframe.setAttribute('allow', allow);
      // Width/height/position are driven entirely by the style effect below
      // (so fullscreen can absolute-fill the iframe within its host without a
      // `w-full` class fighting the inset sizing).
      iframe.className = 'block border-0 bg-white';
      iframe.style.width = '100%';
      iframe.style.height = `${this.frameHeight()}px`;
      // Append BEFORE setting src so contentWindow exists, then start the
      // bridge so the host listener is registered before the proxy script
      // posts its `sandbox-proxy-ready` notification. Doing this in the
      // (load) callback races: the proxy fires ready as soon as its IIFE
      // runs, which is before the host's load event handler dispatches —
      // miss that and the inner App iframe is never mounted (blank frame).
      host.nativeElement.appendChild(iframe);
      this.iframeEl = iframe;
      this.startBridge();
      iframe.src = url;
    });
    // Size + position the iframe per display mode. Inline: a normal block
    // tracking the App's reported height (`size-changed`). Fullscreen: the
    // iframe absolute-fills its flex-1 host below the title bar (the container
    // is the fixed full-viewport overlay at z-[9999] — see `containerClasses`).
    //
    // An <iframe> is a REPLACED element: with width/height:auto it falls back
    // to its intrinsic size (~300x150) and ignores right/bottom insets, so
    // `inset:0` alone leaves a small sliver. It must get explicit dimensions;
    // `100%` resolves against the host's used height (definite via flex-1 +
    // min-h-0) and excludes the page scrollbar gutter.
    effect(() => {
      const h = this.frameHeight();
      const mode = this.displayMode();
      const el = this.iframeEl;
      if (!el) return;
      if (mode === 'fullscreen') {
        el.style.position = 'absolute';
        el.style.top = '0';
        el.style.left = '0';
        el.style.right = '';
        el.style.bottom = '';
        el.style.width = '100%';
        el.style.height = '100%';
        el.style.zIndex = '';
      } else {
        el.style.position = '';
        el.style.top = '';
        el.style.left = '';
        el.style.right = '';
        el.style.bottom = '';
        el.style.zIndex = '';
        el.style.width = '100%';
        el.style.height = `${h}px`;
      }
    });
    // Lock background scroll while fullscreen so the page scrollbar gutter
    // doesn't show beside the overlay and the chat can't scroll behind it.
    // Per-frame save/restore: only ever restore the value this frame saved.
    effect(() => {
      const fullscreen = this.displayMode() === 'fullscreen';
      const body = this.doc.body;
      if (!body) return;
      if (fullscreen && this.lockedBodyOverflow === null) {
        this.lockedBodyOverflow = body.style.overflow;
        body.style.overflow = 'hidden';
      } else if (!fullscreen && this.lockedBodyOverflow !== null) {
        body.style.overflow = this.lockedBodyOverflow;
        this.lockedBodyOverflow = null;
      }
    });
    this.destroyRef.onDestroy(() => {
      // Restore scroll if torn down while still fullscreen.
      if (this.lockedBodyOverflow !== null && this.doc.body) {
        this.doc.body.style.overflow = this.lockedBodyOverflow;
        this.lockedBodyOverflow = null;
      }
      this.bridge?.dispose('component-destroyed');
    });
  }

  private startBridge(): void {
    const res = this.resource();
    if (!res || this.bridge || !this.win) return;
    // Hand the bridge a resource whose permissions are already narrowed to
    // what the user consented to, so sandbox-resource-ready + the
    // initialize `hostCapabilities.sandbox.permissions` advertise only the
    // granted subset (consistent with the outer iframe's `allow`).
    const effectiveRes = { ...res, permissions: this.effectivePermissions() };
    this.bridge = new McpAppBridge({
      hostWindow: this.win,
      getProxyWindow: () => this.iframeEl?.contentWindow ?? null,
      sandboxOrigin: res.sandboxOrigin.replace(/\/$/, ''),
      resource: effectiveRes,
      nonce: this.nonce,
      getToolInput: () => this.resolvedToolInput(),
      // Seeds the latest accumulated partial when the bridge handshake
      // completes, catching the App up to the current streamed state in one
      // shot. The relay effect skips partials that arrive before init (they'd
      // otherwise pile into `preInitQueue` and flush as a burst), so this seed
      // is how the App picks up the in-progress tour; subsequent partials then
      // stream straight through.
      getPartialToolInput: () => this.partialInput() ?? null,
      isToolInputFinal: () => this.inputFinal(),
      getToolResult: () => this.toCallToolResult(),
      getHostContext: () => ({
        theme: this.theme.theme(),
        locale: this.win?.navigator?.language,
        userAgent: 'agentcore-public-stack',
      }),
      openLink: (url) => {
        this.win?.open(url, '_blank', 'noopener,noreferrer');
      },
      proxyToolCall: (toolName, args) =>
        this.mcpAppProxy.proxyToolCall(
          this.toolUseId() ?? '',
          toolName,
          args,
        ),
      sendMessage: (text) => {
        // Mirror the composer's user-turn affordances that a direct
        // submitChatRequest() would otherwise skip: show the loading indicator
        // and scroll the new user message to the top. The user message is
        // added synchronously inside submitChatRequest, so requesting the
        // scroll right after means it already exists in the list.
        this.chatState.setChatLoading(true);
        const result = this.chatRequest.submitChatRequest(
          text,
          this.conversation.currentSession().sessionId || null,
        );
        this.chatState.requestScrollToLastUser();
        return result;
      },
      updateModelContext: (payload) =>
        this.mcpAppMessage.updateModelContext(res.resourceUri, payload),
      requestConsent: (req) => {
        const { id, granted } = this.mcpAppConsent.request(req);
        this.openPromptId.set(id);
        return granted.finally(() => this.openPromptId.set(null));
      },
      requestDisplayMode: (mode) => {
        // This host supports inline + fullscreen; anything else (pip) stays
        // inline. Return the mode actually applied — the bridge relays it
        // back to the App as the resulting mode.
        const resulting: DisplayMode = mode === 'fullscreen' ? 'fullscreen' : 'inline';
        this.displayMode.set(resulting);
        return resulting;
      },
    });
    this.bridge.onSizeChanged((_w, h) => {
      if (h > 0) this.frameHeight.set(Math.ceil(h));
    });
    this.bridge.start();
  }

  /**
   * Host-initiated exit from fullscreen (Exit button / Escape). Collapses
   * back to inline and tells the App via `host-context-changed` so it can
   * re-render its inline affordances.
   */
  protected exitFullscreen(): void {
    if (this.displayMode() === 'inline') return;
    this.displayMode.set('inline');
    this.bridge?.notifyDisplayMode('inline');
  }

  /** Complete tool-call arguments, found by toolUseId in the live stream. */
  private lookupToolInput(): Record<string, unknown> {
    const id = this.toolUseId();
    if (!id) return {};
    for (const msg of this.streamParser.allMessages()) {
      for (const block of msg.content ?? []) {
        const tu = (block as { toolUse?: { toolUseId?: string; input?: unknown } })
          .toolUse;
        if (tu && tu.toolUseId === id && tu.input && typeof tu.input === 'object') {
          return tu.input as Record<string, unknown>;
        }
      }
    }
    return {};
  }

  /** Map the renderer's `ToolResultData` to an MCP `CallToolResult`. */
  private toCallToolResult(): unknown | null {
    const r = this.result();
    if (!r) return null;
    const content = (r.content ?? []).map((item) => {
      if (item.image) {
        return {
          type: 'image',
          data: item.image.data,
          mimeType: `image/${item.image.format}`,
        };
      }
      if (item.json !== undefined) {
        return { type: 'text', text: JSON.stringify(item.json) };
      }
      return { type: 'text', text: item.text ?? '' };
    });
    return { content, isError: r.status === 'error' };
  }
}
