import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  signal,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowPath, heroLockClosed } from '@ng-icons/heroicons/outline';
import { Tool, ToolService } from '../../../services/tool/tool.service';
import { splitToolDescription } from '../../../shared/utils/tool-description';

/** Which panel of the detail view is showing. */
export type ToolDetailTab = 'tools' | 'about';

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
 * Deliberately absent: MCP **prompts** and **resources**. The backend only ever
 * calls `tools/list` (see `app_api/tools/discovery.py`); there is no
 * `prompts/list` or `resources/list` call anywhere in the stack, so a tab for
 * either would be an empty promise. The strip is built to take them once
 * discovery exists.
 */
@Component({
  selector: 'app-tool-detail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroArrowPath, heroLockClosed })],
  host: { class: 'flex h-full flex-col' },
  templateUrl: './tool-detail.component.html',
})
export class ToolDetailComponent {
  protected readonly toolService = inject(ToolService);

  readonly tool = input.required<Tool>();

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

  /**
   * The server's tools with their docstrings split into a readable summary and
   * the reference detail below it. One unsplit description filled two thirds of
   * this pane on Student MyBoiseState, which has seventeen of them.
   */
  protected readonly subTools = computed(() =>
    (this.tool().serverTools ?? []).map((sub) => ({
      ...sub,
      ...splitToolDescription(sub.description),
    })),
  );

  /** Sub-tools whose reference detail the user has opened. */
  private readonly expandedDetails = signal<Set<string>>(new Set());

  isDetailExpanded(name: string): boolean {
    return this.expandedDetails().has(name);
  }

  toggleDetail(name: string): void {
    this.expandedDetails.update((set) => {
      const next = new Set(set);
      if (next.has(name)) {
        next.delete(name);
      } else {
        next.add(name);
      }
      return next;
    });
  }

  protected readonly tabs = computed(() => [
    { id: 'tools' as const, label: 'Tools', count: this.subTools().length },
    { id: 'about' as const, label: 'About', count: null },
  ]);

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
