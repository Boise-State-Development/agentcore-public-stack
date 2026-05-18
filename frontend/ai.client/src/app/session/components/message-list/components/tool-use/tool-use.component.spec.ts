import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { ToolUseComponent } from './tool-use.component';
import {
  ToolRendererRegistryService,
  ToolResultData,
  ToolResultRenderer,
} from './tool-renderer-registry.service';
import { CalculatorToolResultComponent } from './renderers/calculator-tool-result.component';
import { ContentBlock } from '../../../../services/models/message.model';

@Component({
  selector: 'app-spec-stub-renderer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<div class="spec-stub">STUB RENDERER</div>`,
})
class SpecStubRendererComponent implements ToolResultRenderer {
  result = input.required<ToolResultData>();
  minimized = input<boolean>(false);
}

function makeBlock(
  name: string,
  result: ToolResultData['content'],
): ContentBlock {
  return {
    type: 'toolUse',
    toolUse: {
      toolUseId: `tool-${name}`,
      name,
      input: { query: 'test' },
      status: 'complete',
      result: { status: 'success', content: result },
    },
  };
}

describe('ToolUseComponent', () => {
  let fixture: ComponentFixture<ToolUseComponent>;
  let registry: ToolRendererRegistryService;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [ToolUseComponent],
    }).compileComponents();

    registry = TestBed.inject(ToolRendererRegistryService);
    fixture = TestBed.createComponent(ToolUseComponent);
  });

  it('renders a non-migrated tool through the default renderer', () => {
    fixture.componentRef.setInput(
      'toolUse',
      makeBlock('some_unregistered_tool', [{ text: 'default path output' }]),
    );
    fixture.detectChanges();

    const def = fixture.nativeElement.querySelector('app-default-tool-result');
    expect(def).toBeTruthy();
    expect(def.textContent).toContain('default path output');
  });

  it('renders a migrated tool through its registered renderer (identical output)', () => {
    registry.register('calculator', CalculatorToolResultComponent);

    fixture.componentRef.setInput(
      'toolUse',
      makeBlock('calculator', [{ text: 'the answer is 42' }]),
    );
    fixture.detectChanges();

    // The registered proof-point component is used...
    const calc = fixture.nativeElement.querySelector('app-calculator-tool-result');
    expect(calc).toBeTruthy();
    // ...and it delegates to the default renderer, so output is unchanged.
    const def = calc.querySelector('app-default-tool-result');
    expect(def).toBeTruthy();
    expect(calc.textContent).toContain('the answer is 42');
  });

  it('renders identical result text for a migrated vs default-path tool', () => {
    registry.register('calculator', CalculatorToolResultComponent);

    fixture.componentRef.setInput(
      'toolUse',
      makeBlock('calculator', [{ text: 'shared output' }]),
    );
    fixture.detectChanges();
    const migratedText = fixture.nativeElement
      .querySelector('.whitespace-pre-wrap')
      .textContent.trim();

    const otherFixture = TestBed.createComponent(ToolUseComponent);
    otherFixture.componentRef.setInput(
      'toolUse',
      makeBlock('plain_tool', [{ text: 'shared output' }]),
    );
    otherFixture.detectChanges();
    const defaultText = otherFixture.nativeElement
      .querySelector('.whitespace-pre-wrap')
      .textContent.trim();

    expect(migratedText).toBe(defaultText);
    expect(migratedText).toBe('shared output');
  });

  it('uses a custom registered renderer instead of the default', () => {
    registry.register('weird_tool', SpecStubRendererComponent);

    fixture.componentRef.setInput(
      'toolUse',
      makeBlock('weird_tool', [{ text: 'ignored by stub' }]),
    );
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.spec-stub')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('app-default-tool-result')).toBeNull();
  });

  it('does not render a result section when the tool has no result', () => {
    fixture.componentRef.setInput('toolUse', {
      type: 'toolUse',
      toolUse: {
        toolUseId: 'tool-pending',
        name: 'pending_tool',
        input: {},
        status: 'pending',
      },
    } as ContentBlock);
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('app-default-tool-result')).toBeNull();
  });
});
