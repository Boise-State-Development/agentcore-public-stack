import {
  Component,
  input,
  signal,
  computed,
  inject,
  ChangeDetectionStrategy,
} from '@angular/core';
import { NgComponentOutlet } from '@angular/common';
import { JsonSyntaxHighlightPipe } from './json-syntax-highlight.pipe';
import { ContentBlock, ToolUseData } from '../../../../services/models/message.model';
import { ToolRendererRegistryService } from './tool-renderer-registry.service';
import { McpAppStateService } from '../../../../services/mcp-apps/mcp-app-state.service';
import { McpAppFrameComponent } from './renderers/mcp-app-frame.component';

@Component({
  selector: 'app-tool-use',
  templateUrl: './tool-use.component.html',
  styleUrl: './tool-use.component.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [JsonSyntaxHighlightPipe, NgComponentOutlet],
})
export class ToolUseComponent {
  /** The content block containing tool use data */
  toolUse = input.required<ContentBlock>();

  /** Whether the component is displayed in minimized mode (for promoted visuals) */
  minimized = input<boolean>(false);

  /** Whether the details section is expanded (both input and output) */
  isDetailsExpanded = signal(false);

  private readonly rendererRegistry = inject(ToolRendererRegistryService);
  private readonly mcpAppState = inject(McpAppStateService);

  /** Extract tool use data from the content block */
  toolUseData = computed(() => {
    const block = this.toolUse();
    if (block.toolUse) {
      return block.toolUse as ToolUseData;
    }
    // Fallback for legacy format (if toolUse is nested differently)
    return block as unknown as ToolUseData;
  });

  /** Tool name */
  toolName = computed(() => {
    return this.toolUseData().name || 'Unknown Tool';
  });

  /** Originating tool-use id (correlates the MCP App resource + tool data). */
  toolUseId = computed(() => this.toolUseData().toolUseId);

  /** Tool input */
  toolInput = computed(() => {
    return this.toolUseData().input || {};
  });

  /** Tool execution status */
  toolStatus = computed(() => {
    return this.toolUseData().status || 'pending';
  });

  /** Tool result */
  toolResult = computed(() => {
    return this.toolUseData().result;
  });

  /** Check if tool has a result */
  hasResult = computed(() => {
    return !!this.toolResult();
  });

  /**
   * Result-renderer component for this tool.
   *
   * An MCP App (SEP-1865) takes precedence: when this tool invocation
   * produced a `ui_resource` event, render the sandbox-proxy frame. App
   * tool names are server-defined and per-invocation, so this can't be a
   * static name→component registry entry (PR #0) — it keys off the
   * toolUseId-scoped resource instead, but stays a single
   * `[ngComponentOutlet]` with no template branch. Otherwise fall back to
   * the name-keyed registry (default = text/JSON/image). Both reads are
   * signals, so this stays reactive to a `ui_resource` arriving after the
   * tool-use block first renders.
   */
  resultRenderer = computed(() =>
    this.mcpAppState.has(this.toolUseId())
      ? McpAppFrameComponent
      : this.rendererRegistry.resolve(this.toolName()),
  );

  /**
   * Inputs bound onto the resolved renderer via NgComponentOutlet.
   * `toolUseId` is passed ONLY to the MCP App frame: NgComponentOutlet
   * throws (NG0303) if asked to set an input a component doesn't declare,
   * and the default/other renderers intentionally don't expose it.
   */
  rendererInputs = computed(() => {
    const base = {
      result: this.toolResult(),
      minimized: this.minimized(),
    };
    return this.resultRenderer() === McpAppFrameComponent
      ? { ...base, toolUseId: this.toolUseId() }
      : base;
  });

  /** Preview of input keys for collapsed state */
  inputKeysPreview = computed(() => {
    const keys = Object.keys(this.toolInput());
    if (keys.length === 0) {
      return '{ }';
    }
    return `{ ${keys.join(', ')} }`;
  });

  /** Formatted JSON string for input display */
  formattedJson = computed(() => {
    return JSON.stringify(this.toolInput(), null, 2);
  });

  /** Check if input is empty */
  hasInput = computed(() => {
    return Object.keys(this.toolInput()).length > 0;
  });

  /** Status text based on tool status */
  statusText = computed(() => {
    const statusMap: Record<string, string> = {
      pending: 'Running...',
      complete: 'Complete',
      error: 'Failed',
    };
    return statusMap[this.toolStatus()] || 'Unknown';
  });

  /** Status icon color classes */
  statusIconColor = computed(() => {
    const status = this.toolStatus();
    if (status === 'complete') return 'text-green-600 dark:text-green-400';
    if (status === 'error') return 'text-red-600 dark:text-red-400';
    return 'text-blue-600 dark:text-blue-400';
  });

  /** Toggle the details expanded state */
  toggleDetailsExpanded(): void {
    this.isDetailsExpanded.update((expanded) => !expanded);
  }
}
