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
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import type {
  ToolResultData,
  ToolResultRenderer,
} from '../tool-renderer-registry.service';
import { McpAppStateService } from '../../../../../services/mcp-apps/mcp-app-state.service';
import { StreamParserService } from '../../../../../services/chat/stream-parser.service';
import { ThemeService } from '../../../../../../components/topnav/components/theme-toggle/theme.service';
import { McpAppBridge } from '../../../../../services/mcp-apps/mcp-app-bridge';
import { McpAppProxyService } from '../../../../../services/mcp-apps/mcp-app-proxy.service';

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
  styles: ':host { display: block; }',
  template: `
    @if (safeProxyUrl(); as url) {
      <div
        class="overflow-hidden rounded-sm border border-gray-300 dark:border-gray-600 bg-white"
      >
        <iframe
          #frame
          [src]="url"
          [style.height.px]="frameHeight()"
          [attr.allow]="allowAttr()"
          class="block w-full border-0 bg-white"
          title="MCP App"
          sandbox="allow-scripts allow-same-origin"
          referrerpolicy="no-referrer"
          loading="lazy"
          (load)="onProxyLoad()"
        ></iframe>
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
  private readonly streamParser = inject(StreamParserService);
  private readonly theme = inject(ThemeService);
  private readonly sanitizer = inject(DomSanitizer);
  private readonly destroyRef = inject(DestroyRef);
  private readonly win = inject(DOCUMENT).defaultView;

  private readonly frameRef =
    viewChild<ElementRef<HTMLIFrameElement>>('frame');

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

  protected readonly safeProxyUrl = computed<SafeResourceUrl | null>(() => {
    const res = this.resource();
    if (!res || !res.sandboxOrigin) return null;
    // Trusted single value from our authenticated backend (SSM-sourced);
    // the sandbox attribute + the proxy's per-resource CSP are the real
    // containment, same justification as the artifact panel.
    return this.sanitizer.bypassSecurityTrustResourceUrl(
      `${res.sandboxOrigin.replace(/\/$/, '')}/proxy.html`,
    );
  });

  /** Permissions-Policy `allow` for the outer frame (delegates to inner). */
  protected readonly allowAttr = computed(() => {
    const p = this.resource()?.permissions ?? {};
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
    this.destroyRef.onDestroy(() => this.bridge?.dispose('component-destroyed'));
  }

  protected onProxyLoad(): void {
    const res = this.resource();
    if (!res || this.bridge || !this.win) return;
    this.bridge = new McpAppBridge({
      hostWindow: this.win,
      getProxyWindow: () => this.frameRef()?.nativeElement.contentWindow ?? null,
      sandboxOrigin: res.sandboxOrigin.replace(/\/$/, ''),
      resource: res,
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
