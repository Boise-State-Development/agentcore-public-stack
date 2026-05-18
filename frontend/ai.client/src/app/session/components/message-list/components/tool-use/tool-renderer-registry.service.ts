import { Injectable, InputSignal, Type, signal } from '@angular/core';
import { ToolResultContent } from '../../../../services/models/message.model';
import { DefaultToolResultComponent } from './renderers/default-tool-result.component';

/**
 * The tool result payload handed to every renderer. Mirrors the inline
 * `result` shape on {@link ToolUseData} so renderers don't depend on the
 * surrounding content-block envelope.
 */
export interface ToolResultData {
  content: ToolResultContent[];
  status: 'success' | 'error';
}

/**
 * Structural contract every registered tool-result renderer must satisfy.
 * A renderer is just a standalone component that exposes these two signal
 * inputs; the host binds them via `NgComponentOutlet`. The future MCP App
 * renderer plugs in as one of these with no host-template changes.
 */
export interface ToolResultRenderer {
  readonly result: InputSignal<ToolResultData>;
  readonly minimized: InputSignal<boolean>;
}

/**
 * Signal-backed lookup of tool name → result-renderer component.
 *
 * Unregistered tools resolve to {@link DefaultToolResultComponent}, which
 * reproduces the historical text/JSON/image rendering verbatim. `resolve`
 * reads the backing signal, so a `computed()` that calls it stays reactive
 * to registrations that happen after first render.
 */
@Injectable({ providedIn: 'root' })
export class ToolRendererRegistryService {
  private readonly renderers = signal<ReadonlyMap<string, Type<ToolResultRenderer>>>(
    new Map(),
  );

  register(toolName: string, component: Type<ToolResultRenderer>): void {
    const next = new Map(this.renderers());
    next.set(toolName, component);
    this.renderers.set(next);
  }

  resolve(toolName: string): Type<ToolResultRenderer> {
    return this.renderers().get(toolName) ?? DefaultToolResultComponent;
  }

  has(toolName: string): boolean {
    return this.renderers().has(toolName);
  }
}
