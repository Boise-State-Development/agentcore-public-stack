import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  linkedSignal,
  signal,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowPath, heroCheckCircle, heroLockClosed } from '@ng-icons/heroicons/outline';
import { Tool, ToolService } from '../../../services/tool/tool.service';
import { ToolCapabilityService } from '../../../services/tool-capability/tool-capability.service';
import { ConnectorStatusService } from '../../../settings/connectors/services/connector-status.service';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';

/** Which panel of the detail view is showing. */
export type ToolDetailTab = 'tools' | 'prompts' | 'resources' | 'about';

/**
 * The second pane of the tools drawer: everything about one tool that a row
 * 320px wide cannot hold.
 *
 * Why a pane and not an inline expansion: the drawer used to expand an MCP
 * server's per-tool toggles *into the row itself*, so Student MyBoiseState
 * pushed 17 nested rows into a column narrower than a phone. The list and this
 * pane now sit side by side in one clipped stack and slide as a pair — the
 * detail gets the drawer's full width, and the list keeps its scroll position
 * underneath.
 *
 * Prompts and resources come from the stored capability snapshot, never a live
 * probe: probing opens an MCP session per server, and a 3LO server cannot be
 * reached without a consent token the browser does not hold. An admin refreshes
 * the snapshot; this pane reads it.
 *
 * They are read-only here. Acting on them needs two MCP methods the backend
 * does not expose yet — `prompts/get` to expand a template's arguments into
 * real text, and `resources/read` to fetch a resource's contents — so an
 * "Insert" that pasted a prompt's *name* into the composer would be a worse
 * answer than none.
 */
@Component({
  selector: 'app-tool-detail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroArrowPath, heroCheckCircle, heroLockClosed })],
  host: { class: 'flex h-full flex-col' },
  templateUrl: './tool-detail.component.html',
})
export class ToolDetailComponent {
  protected readonly toolService = inject(ToolService);
  private readonly capabilityService = inject(ToolCapabilityService);
  protected readonly connectorStatus = inject(ConnectorStatusService);
  private readonly consent = inject(OAuthConsentService);

  readonly tool = input.required<Tool>();

  constructor() {
    // Load the snapshot when a tool is shown. Keyed on the id so drilling into
    // a second server fetches its own; `ensure` is a no-op for one already
    // cached, so re-opening the same tool costs nothing.
    effect(() => {
      void this.capabilityService.ensure(this.tool().toolId);
    });
  }

  /** The OAuth provider this tool needs, or null when it needs none. */
  protected readonly providerId = computed(() => this.tool().requiresOauthProvider ?? null);

  /** Connection state for that provider; 'none' when the tool needs no consent. */
  protected readonly connection = computed(() => {
    const provider = this.providerId();
    return provider ? this.connectorStatus.stateFor(provider) : 'none';
  });

  protected readonly connecting = computed(() => {
    const provider = this.providerId();
    return provider ? this.consent.inFlightProviders().has(provider) : false;
  });

  /**
   * Open the provider's consent popup. Reuses the same service the chat layer
   * and settings page use, so popup blocking, COOP-severed openers and the
   * completion broadcast are all already handled — and
   * `ConnectorStatusService` flips the chip when that broadcast lands.
   *
   * `requestConsent` is called with no authorization URL on purpose: the
   * service then fetches a fresh one, because AgentCore's URLs expire quickly.
   */
  protected connect(): void {
    const provider = this.providerId();
    if (!provider) return;
    this.consent.requestConsent(provider, undefined);
    void this.consent.openConsentPopup(provider);
  }

  protected readonly isMcpServer = computed(() => {
    const protocol = this.tool().protocol;
    return protocol === 'mcp' || protocol === 'mcp_external';
  });

  /**
   * Resets whenever a *different tool* is shown, so you never land on the tab
   * you left open on the previous one. A non-MCP tool has no Tools panel, so it
   * starts on About.
   *
   * The source has to be the tool id, not the computation's own reads. Written
   * as a bare `linkedSignal(() => this.isMcpServer() ? ... )` it only reset when
   * the *protocol* changed — so Gmail → Canvas, both `mcp_external`, kept the
   * stale tab.
   */
  protected readonly tab = linkedSignal<string, ToolDetailTab>({
    source: () => this.tool().toolId,
    computation: () => (this.isMcpServer() ? 'tools' : 'about'),
  });

  protected readonly discovering = signal(false);
  protected readonly discoverError = signal<string | null>(null);

  protected readonly subTools = computed(() => this.tool().serverTools ?? []);

  /** The stored snapshot for this tool, or null while loading / on failure. */
  protected readonly capabilities = computed(() =>
    this.capabilityService.capabilitiesFor(this.tool().toolId),
  );

  protected readonly capabilitiesLoading = computed(() =>
    this.capabilityService.isLoading(this.tool().toolId),
  );

  protected readonly prompts = computed(() => this.capabilities()?.prompts ?? []);
  protected readonly resources = computed(() => this.capabilities()?.resources ?? []);

  /** Never discovered — distinct from "discovered and offers nothing". */
  protected readonly neverDiscovered = computed(() => {
    const snapshot = this.capabilities();
    return !!snapshot && !snapshot.discoveredAt;
  });

  /**
   * Prompts and resources are only offered for an external MCP server. A local
   * tool has no server to ask, and a Gateway target exposes tools only.
   */
  protected readonly tabs = computed(() => {
    const tabs: { id: ToolDetailTab; label: string; count: number | null }[] = [
      { id: 'tools', label: 'Tools', count: this.subTools().length },
    ];
    if (this.tool().protocol === 'mcp_external') {
      tabs.push(
        { id: 'prompts', label: 'Prompts', count: this.prompts().length },
        { id: 'resources', label: 'Resources', count: this.resources().length },
      );
    }
    tabs.push({ id: 'about', label: 'About', count: null });
    return tabs;
  });

  /** Initials, so a row reads as an object rather than a line of text. */
  protected readonly monogram = computed(() => {
    const initials = this.tool()
      .displayName.replace(/[^A-Za-z ]/g, '')
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((word) => word[0])
      .join('');
    return initials.toUpperCase() || '?';
  });

  /** Catalog facts the drawer row has no room for. */
  protected readonly facts = computed(() => {
    const tool = this.tool();
    const rows: { label: string; value: string }[] = [
      { label: 'Tool ID', value: tool.toolId },
      { label: 'Protocol', value: tool.protocol },
      { label: 'Category', value: tool.category },
      { label: 'Status', value: tool.status },
      { label: 'On by default', value: tool.enabledByDefault ? 'yes' : 'no' },
    ];
    if (tool.grantedBy.length > 0) {
      rows.push({ label: 'Granted by', value: tool.grantedBy.join(', ') });
    }
    return rows;
  });

  protected toggleTool(): void {
    this.toolService.toggleTool(this.tool().toolId).catch((err) => {
      console.error('Failed to toggle tool:', err);
    });
  }

  protected toggleServerTool(name: string): void {
    this.toolService.toggleServerTool(this.tool().toolId, name).catch((err) => {
      console.error('Failed to toggle server tool:', err);
    });
  }

  /** Roving-tabindex arrow navigation, as the tablist pattern requires. */
  protected onTabKeydown(event: KeyboardEvent): void {
    const ids = this.tabs().map((item) => item.id);
    const current = ids.indexOf(this.tab());
    let next = current;

    if (event.key === 'ArrowRight') next = (current + 1) % ids.length;
    else if (event.key === 'ArrowLeft') next = (current - 1 + ids.length) % ids.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = ids.length - 1;
    else return;

    event.preventDefault();
    this.tab.set(ids[next]);
    document.getElementById('tool-detail-tab-' + ids[next])?.focus();
  }

  protected async discover(): Promise<void> {
    this.discovering.set(true);
    this.discoverError.set(null);
    try {
      await this.toolService.discoverServerTools(this.tool().toolId);
    } catch {
      this.discoverError.set('Could not list this server’s tools.');
    } finally {
      this.discovering.set(false);
    }
  }
}
