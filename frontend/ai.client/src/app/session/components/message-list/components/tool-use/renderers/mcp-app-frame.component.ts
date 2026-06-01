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
import type {
  CapabilityKey,
  DisplayMode,
} from '../../../../../services/mcp-apps/mcp-app-protocol';
import { ChatRequestService } from '../../../../../services/chat/chat-request.service';
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
  imports: [McpAppConsentPromptComponent],
  styles: ':host { display: block; }',
  template: `
    @if (currentPrompt(); as prompt) {
      <div class="mb-2 flex justify-start">
        <app-mcp-app-consent-prompt [prompt]="prompt" />
      </div>
    }
    @if (proxyUrl(); as url) {
      <div
        [class]="containerClasses()"
        [attr.role]="displayMode() === 'fullscreen' ? 'dialog' : null"
        [attr.aria-modal]="displayMode() === 'fullscreen' ? 'true' : null"
        [attr.aria-label]="displayMode() === 'fullscreen' ? 'MCP App, fullscreen' : null"
        (keydown.escape)="exitFullscreen()"
      >
        @if (displayMode() === 'fullscreen') {
          <button
            type="button"
            class="fixed top-3 right-3 z-[10000] rounded-md bg-gray-900/80 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500"
            (click)="exitFullscreen()"
          >
            Exit fullscreen
          </button>
        }
        <div #host class="block"></div>
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

  private readonly mcpAppState = inject(McpAppStateService);
  private readonly mcpAppProxy = inject(McpAppProxyService);
  private readonly mcpAppMessage = inject(McpAppMessageService);
  private readonly mcpAppConsent = inject(McpAppConsentService);
  private readonly chatRequest = inject(ChatRequestService);
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
   * iframe ITSELF is promoted to a fixed full-viewport overlay (see the
   * style effect) — sizing it via its own insets avoids a percentage-height
   * chain, and a CSS change (unlike a DOM move) never reloads the iframe, so
   * the running App keeps its state.
   */
  protected readonly displayMode = signal<DisplayMode>('inline');

  /**
   * Outer wrapper classes. In fullscreen the iframe is lifted out of flow
   * (fixed), so the wrapper collapses behind the overlay — drop its border
   * and rounding so nothing peeks through.
   */
  protected readonly containerClasses = computed(() =>
    this.displayMode() === 'fullscreen'
      ? 'relative'
      : 'relative overflow-hidden rounded-sm border border-gray-300 bg-white dark:border-gray-600',
  );

  private bridge: McpAppBridge | null = null;
  private readonly nonce =
    this.win?.crypto?.randomUUID?.() ?? `n-${Math.random().toString(36).slice(2)}`;

  /** The UI resource for this tool invocation (undefined ⇒ render nothing). */
  protected readonly resource = computed(() => {
    const id = this.toolUseId();
    return id ? this.mcpAppState.get(id) : undefined;
  });

  /** Capabilities the resource declares (`_meta.ui.permissions`). */
  private readonly requestedCaps = computed<CapabilityKey[]>(() => {
    const p = this.resource()?.permissions ?? {};
    const caps: CapabilityKey[] = [];
    if (p.camera) caps.push('camera');
    if (p.microphone) caps.push('microphone');
    if (p.geolocation) caps.push('geolocation');
    if (p.clipboardWrite) caps.push('clipboardWrite');
    return caps;
  });

  /**
   * Render-time capability consent (PR #6). `null` = undecided. When the
   * resource requests sensitive sandbox features we hold the frame until
   * the user answers an inline prompt, then grant only what was approved
   * (all-or-nothing for v1). Frontend-only — the request never reaches a
   * backend turn, same justification as `McpAppConsentService`.
   */
  private readonly capabilityGrant = signal<boolean | null>(null);
  private capabilityAsked = false;

  /** Id of this frame's currently-open consent prompt (capability ask or
   *  open-link), used to render it inline above the iframe instead of in
   *  an unanchored message-list strip. */
  private readonly openPromptId = signal<string | null>(null);
  protected readonly currentPrompt = computed(() => {
    const id = this.openPromptId();
    if (!id) return null;
    return this.mcpAppConsent.pending().find((p) => p.id === id) ?? null;
  });

  /** True once requested capabilities are decided (or none were asked). */
  private readonly capabilitiesResolved = computed(
    () => this.requestedCaps().length === 0 || this.capabilityGrant() !== null,
  );

  /** Permissions actually applied to the frame: declared ∩ consent. */
  private readonly effectivePermissions = computed(() => {
    const declared = this.resource()?.permissions ?? {};
    if (this.requestedCaps().length === 0) return declared;
    return this.capabilityGrant() ? declared : {};
  });

  /**
   * Plain string URL the iframe `src` is set to. Stays null until consent
   * resolves so the @if-gated host div doesn't appear. Trusted single
   * value from our authenticated backend (SSM-sourced); the imperative
   * sandbox attribute + the proxy's per-resource CSP are the real
   * containment, same justification as the artifact panel.
   *
   * The `?csp=` query the proxy CFN reads is built from the resource's
   * declared `_meta.ui.csp` (`buildProxyUrl`). Apps that declare nothing
   * get the bare URL and the proxy's default CSP — no cache fragmentation
   * for the no-declaration majority.
   */
  protected readonly proxyUrl = computed<string | null>(() => {
    const res = this.resource();
    if (!res || !res.sandboxOrigin) return null;
    if (!this.capabilitiesResolved()) return null;
    return buildProxyUrl(res.sandboxOrigin, res.csp);
  });

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
    // Re-push the tool result if it lands/changes after the App initialized.
    effect(() => {
      this.result();
      this.bridge?.refreshToolResult();
    });
    // Render-time capability consent: when the resource requests sensitive
    // sandbox features, ask once and hold the frame until answered.
    effect(() => {
      const caps = this.requestedCaps();
      if (caps.length === 0 || this.capabilityAsked) return;
      this.capabilityAsked = true;
      const { id, granted } = this.mcpAppConsent.request({
        kind: 'capabilities',
        capabilities: caps,
      });
      this.openPromptId.set(id);
      granted
        .then((g) => this.capabilityGrant.set(g))
        .catch(() => this.capabilityGrant.set(false))
        .finally(() => this.openPromptId.set(null));
    });
    // Imperatively create the iframe once the host div mounts and consent
    // is resolved. Angular 21 forbids dynamic `[attr.allow]` on <iframe>
    // (NG0910), so we build the element by hand with all attributes set
    // before src — the browser only consults `allow` at load-start.
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
      // (so fullscreen can promote the iframe itself to a fixed overlay
      // without a `w-full` class fighting the inset sizing).
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
    // iframe itself becomes a fixed full-viewport overlay at z-[9999] (the
    // app's top modal layer — matching the image/markdown lightboxes).
    //
    // An <iframe> is a REPLACED element: with width/height:auto it falls back
    // to its intrinsic size (~300x150) and ignores right/bottom insets, so
    // `inset:0` alone leaves a small sliver. It must get explicit dimensions.
    // `100%` resolves against the viewport (the ICB is the fixed containing
    // block) and, unlike `100vw/100vh`, excludes the scrollbar gutter.
    effect(() => {
      const h = this.frameHeight();
      const mode = this.displayMode();
      const el = this.iframeEl;
      if (!el) return;
      if (mode === 'fullscreen') {
        el.style.position = 'fixed';
        el.style.top = '0';
        el.style.left = '0';
        el.style.right = '';
        el.style.bottom = '';
        el.style.width = '100%';
        el.style.height = '100%';
        el.style.zIndex = '9999';
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
      getToolInput: () => this.lookupToolInput(),
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
      sendMessage: (text) =>
        this.chatRequest.submitChatRequest(
          text,
          this.conversation.currentSession().sessionId || null,
        ),
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
