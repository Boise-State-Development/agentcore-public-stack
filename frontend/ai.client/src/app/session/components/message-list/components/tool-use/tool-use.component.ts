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
   * Result-renderer component for this tool. Resolved from the name-keyed
   * registry (default = text/JSON/image). MCP Apps (SEP-1865) deliberately
   * do NOT route through here — they render as their own first-class
   * `mcp_app_frame` block in <app-assistant-message>, with the tool card
   * still showing the call's input/output as provenance.
   */
  resultRenderer = computed(() =>
    this.rendererRegistry.resolve(this.toolName()),
  );

  /** Inputs bound onto the resolved renderer via NgComponentOutlet. */
  rendererInputs = computed(() => ({
    result: this.toolResult(),
    minimized: this.minimized(),
  }));

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
