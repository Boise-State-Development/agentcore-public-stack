import { ComponentFixture, TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { ReasoningContentComponent } from './reasoning-content.component';
import { ContentBlock } from '../../../../services/models/message.model';

/**
 * The reasoning header (docs/specs/agent-state-feedback.md PR-1).
 *
 * "Thinking" while the model is at it, "Thought for 17s" once it has stopped,
 * and back to "Thinking" on a reloaded conversation — where the duration was
 * never measured and inventing one would be worse than omitting it.
 */
describe('ReasoningContentComponent — header', () => {
  let fixture: ComponentFixture<ReasoningContentComponent>;

  const block = (durationMs?: number): ContentBlock =>
    ({
      type: 'reasoningContent',
      reasoningContent: { reasoningText: { text: 'considering the options' } },
      ...(durationMs === undefined ? {} : { reasoningDurationMs: durationMs }),
    }) as ContentBlock;

  const headerText = (durationMs?: number): string => {
    fixture.componentRef.setInput('contentBlock', block(durationMs));
    fixture.detectChanges();
    return (
      fixture.nativeElement as HTMLElement
    ).querySelector('button span span')!.textContent!.trim();
  };

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [ReasoningContentComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(ReasoningContentComponent);
  });

  afterEach(() => TestBed.resetTestingModule());

  it('reads "Thinking" while the span is still open', () => {
    expect(headerText(undefined)).toBe('Thinking');
  });

  it('reports whole seconds once the model has stopped', () => {
    expect(headerText(17_000)).toBe('Thought for 17s');
  });

  it('rounds to the nearest second rather than truncating', () => {
    expect(headerText(17_600)).toBe('Thought for 18s');
  });

  it('never rounds a sub-second block up to a full second', () => {
    expect(headerText(400)).toBe('Thought for <1s');
  });

  it('breaks a long think into minutes and seconds', () => {
    expect(headerText(72_000)).toBe('Thought for 1m 12s');
  });

  it('treats a zero-length span as measured, not missing', () => {
    expect(headerText(0)).toBe('Thought for <1s');
  });
});
