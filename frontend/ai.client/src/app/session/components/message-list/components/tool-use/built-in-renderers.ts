import { EnvironmentProviders, inject, provideAppInitializer } from '@angular/core';
import { ToolRendererRegistryService } from './tool-renderer-registry.service';
import { CalculatorToolResultComponent } from './renderers/calculator-tool-result.component';
import { FetchUrlContentToolResultComponent } from './renderers/fetch-url-content-tool-result.component';
import { CreateVisualizationToolResultComponent } from './renderers/create-visualization-tool-result.component';

/**
 * Registers the built-in proof-point renderers into
 * {@link ToolRendererRegistryService} at bootstrap. New renderers (including
 * the future MCP App renderer) register here — or via their own
 * `provideAppInitializer` — with no host-template changes.
 */
export function provideBuiltInToolRenderers(): EnvironmentProviders {
  return provideAppInitializer(() => {
    const registry = inject(ToolRendererRegistryService);
    registry.register('calculator', CalculatorToolResultComponent);
    registry.register('fetch_url_content', FetchUrlContentToolResultComponent);
    registry.register('create_visualization', CreateVisualizationToolResultComponent);
  });
}
