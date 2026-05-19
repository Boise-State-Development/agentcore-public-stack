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
import type { CapabilityKey } from '../../../../../services/mcp-apps/mcp-app-protocol';
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
        #host
        class="overflow-hidden rounded-sm border border-gray-300 dark:border-gray-600 bg-white"
      ></div>
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

  /** Initial height; the App drives it via `ui/notifications/size-changed`. */
  protected readonly frameHeight = signal(360);

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
      iframe.className = 'block w-full border-0 bg-white';
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
    // Keep the iframe height in sync as the App reports size changes.
    effect(() => {
      const h = this.frameHeight();
      if (this.iframeEl) this.iframeEl.style.height = `${h}px`;
    });
    this.destroyRef.onDestroy(() => this.bridge?.dispose('component-destroyed'));
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
    });
    this.bridge.onSizeChanged((_w, h) => {
      if (h > 0) this.frameHeight.set(Math.ceil(h));
    });
    this.bridge.start();
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
