import {
  Component,
  input,
  signal,
  computed,
  ChangeDetectionStrategy,
} from '@angular/core';
import { JsonSyntaxHighlightPipe } from './json-syntax-highlight.pipe';
import { ContentBlock } from '../../../../services/models/message.model';
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
  isExpanded = signal(false);

  /** Extract tool use data from the content block */
  toolUseData = computed(() => {
    const block = this.toolUse();
    if (block.toolUse) {
      return block.toolUse as { name?: string; input?: Record<string, unknown>; toolUseId?: string };
    }
    // Fallback for legacy format (if toolUse is nested differently)
    return block as unknown as { name?: string; input?: Record<string, unknown>; toolUseId?: string };
  });

  /** Tool name */
  toolName = computed(() => {
    return this.toolUseData().name || 'Unknown Tool';
  });

  /** Tool input */
  toolInput = computed(() => {
    return this.toolUseData().input || {};
  });

  /** Preview of input keys for collapsed state */
  inputKeysPreview = computed(() => {
    const keys = Object.keys(this.toolInput());
    if (keys.length === 0) {
      return '{ }';
    }
    return `{ ${keys.join(', ')} }`;
  });

  /** Formatted JSON string for display */
  formattedJson = computed(() => {
    return JSON.stringify(this.toolInput(), null, 2);
  });

  /** Check if input is empty */
  hasInput = computed(() => {
    return Object.keys(this.toolInput()).length > 0;
  });

  /** Status text based on tool status */
  // statusText = computed(() => {
  //   const statusMap: Record<ToolUseStatus, string> = {
  //     loading: 'Running...',
  //     success: 'Done',
  //     error: 'Failed',
  //   };
  //   return statusMap[this.toolUse().status];
  // });

  /** Toggle the expanded state */
  toggleExpanded(): void {
    this.isExpanded.update((expanded) => !expanded);
  }
}
