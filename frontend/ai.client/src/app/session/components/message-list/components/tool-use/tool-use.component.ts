import {
  Component,
  input,
  signal,
  computed,
  ChangeDetectionStrategy,
} from '@angular/core';
import { JsonSyntaxHighlightPipe } from './json-syntax-highlight.pipe';
import { ContentBlock, ToolUseData, ToolResultContent } from '../../../../services/models/message.model';

@Component({
  selector: 'app-tool-use',
  templateUrl: './tool-use.component.html',
  styleUrl: './tool-use.component.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [JsonSyntaxHighlightPipe],
})
export class ToolUseComponent {
  /** The content block containing tool use data */
  toolUse = input.required<ContentBlock>();

  /** Whether the JSON input section is expanded */
  isInputExpanded = signal(false);

  /** Whether the result section is expanded */
  isResultExpanded = signal(true);

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

  /** Get result text content */
  resultTextContent = computed(() => {
    const result = this.toolResult();
    if (!result) return [];

    return result.content.filter(item => item.text || item.json);
  });

  /** Get result image content */
  resultImageContent = computed(() => {
    const result = this.toolResult();
    if (!result) return [];

    return result.content.filter(item => item.image);
  });

  /** Format result content for display */
  formatResultContent(item: ToolResultContent): string {
    if (item.text) return item.text;
    if (item.json) return JSON.stringify(item.json, null, 2);
    return '';
  }

  /** Get image data URL */
  getImageDataUrl(item: ToolResultContent): string {
    if (!item.image) return '';
    return `data:image/${item.image.format};base64,${item.image.data}`;
  }

  /** Toggle the input expanded state */
  toggleInputExpanded(): void {
    this.isInputExpanded.update((expanded) => !expanded);
  }

  /** Toggle the result expanded state */
  toggleResultExpanded(): void {
    this.isResultExpanded.update((expanded) => !expanded);
  }

  /** For backwards compatibility */
  toggleExpanded(): void {
    this.toggleInputExpanded();
  }

  /** Alias for backwards compatibility */
  isExpanded = this.isInputExpanded;
}
