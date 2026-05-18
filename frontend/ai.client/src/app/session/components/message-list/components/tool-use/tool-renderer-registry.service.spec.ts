import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import {
  ToolRendererRegistryService,
  ToolResultData,
  ToolResultRenderer,
} from './tool-renderer-registry.service';
import { DefaultToolResultComponent } from './renderers/default-tool-result.component';

@Component({
  selector: 'app-stub-renderer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<div class="stub-renderer">stub</div>`,
})
class StubRendererComponent implements ToolResultRenderer {
  result = input.required<ToolResultData>();
  minimized = input<boolean>(false);
}

@Component({
  selector: 'app-other-renderer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<div class="other-renderer">other</div>`,
})
class OtherRendererComponent implements ToolResultRenderer {
  result = input.required<ToolResultData>();
  minimized = input<boolean>(false);
}

describe('ToolRendererRegistryService', () => {
  let registry: ToolRendererRegistryService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    registry = TestBed.inject(ToolRendererRegistryService);
  });

  it('resolves unregistered tools to the default renderer', () => {
    expect(registry.resolve('not_registered')).toBe(DefaultToolResultComponent);
    expect(registry.has('not_registered')).toBe(false);
  });

  it('resolves a registered tool to its component', () => {
    registry.register('calculator', StubRendererComponent);

    expect(registry.has('calculator')).toBe(true);
    expect(registry.resolve('calculator')).toBe(StubRendererComponent);
    // Other tools still fall back to the default.
    expect(registry.resolve('fetch_url_content')).toBe(DefaultToolResultComponent);
  });

  it('lets a later registration override an earlier one', () => {
    registry.register('calculator', StubRendererComponent);
    registry.register('calculator', OtherRendererComponent);

    expect(registry.resolve('calculator')).toBe(OtherRendererComponent);
  });

  it('stays reactive inside a computed when a renderer registers late', () => {
    const resolved = computed(() => registry.resolve('calculator'));

    expect(resolved()).toBe(DefaultToolResultComponent);

    registry.register('calculator', StubRendererComponent);

    expect(resolved()).toBe(StubRendererComponent);
  });
});
