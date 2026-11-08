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
    ).querySelector('.reasoning-headline')!.textContent!.trim();
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

describe('ReasoningContentComponent — tool-rail styling', () => {
  let fixture: ComponentFixture<ReasoningContentComponent>;

  const render = (opts: { durationMs?: number; streaming?: boolean; expanded?: boolean } = {}) => {
    fixture.componentRef.setInput('contentBlock', {
      type: 'reasoningContent',
      reasoningContent: { reasoningText: { text: 'considering the options' } },
      ...(opts.durationMs === undefined ? {} : { reasoningDurationMs: opts.durationMs }),
    } as ContentBlock);
    fixture.componentRef.setInput('isStreaming', opts.streaming ?? false);
    if (opts.expanded) fixture.componentInstance.toggleExpanded();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  };

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [ReasoningContentComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(ReasoningContentComponent);
  });

  afterEach(() => TestBed.resetTestingModule());

  it('sweeps the headline only while the model is still thinking', () => {
    expect(render({ streaming: true }).querySelector('.reasoning-headline')!.classList)
      .toContain('text-shimmer');
  });

  it('stops the sweep once the span has a duration', () => {
    expect(render({ streaming: true, durationMs: 4_000 }).querySelector('.reasoning-headline')!.classList)
      .not.toContain('text-shimmer');
  });

  it('never sweeps a reloaded block', () => {
    expect(render().querySelector('.reasoning-headline')!.classList).not.toContain('text-shimmer');
  });

  it('keeps the collapsed panel out of the tab order', () => {
    const host = render();
    const panel = host.querySelector('.collapsible-content')!;
    expect(panel.hasAttribute('inert')).toBe(true);
    expect(host.querySelector('button')!.getAttribute('aria-controls')).toBe(panel.id);
  });

  it('opens the panel and drops the inline preview', () => {
    const host = render({ expanded: true });
    expect(host.querySelector('.collapsible-content')!.hasAttribute('inert')).toBe(false);
    expect(host.querySelector('button')!.getAttribute('aria-expanded')).toBe('true');
    expect(host.querySelector('.reasoning-scroll')!.textContent).toContain('considering the options');
    expect(host.querySelector('button')!.textContent).not.toContain('considering the options');
  });

  it('gives each instance its own panel id', () => {
    const first = render().querySelector('.collapsible-content')!.id;
    const second = TestBed.createComponent(ReasoningContentComponent);
    second.componentRef.setInput('contentBlock', {
      type: 'reasoningContent',
      reasoningContent: { reasoningText: { text: 'x' } },
    } as ContentBlock);
    second.detectChanges();
    expect((second.nativeElement as HTMLElement).querySelector('.collapsible-content')!.id).not.toBe(first);
  });
});

describe('ReasoningContentComponent — preview line', () => {
  let fixture: ComponentFixture<ReasoningContentComponent>;

  const preview = (text: string, opts: { streaming?: boolean; durationMs?: number } = {}): string => {
    fixture.componentRef.setInput('contentBlock', {
      type: 'reasoningContent',
      reasoningContent: { reasoningText: { text } },
      ...(opts.durationMs === undefined ? {} : { reasoningDurationMs: opts.durationMs }),
    } as ContentBlock);
    fixture.componentRef.setInput('isStreaming', opts.streaming ?? false);
    fixture.detectChanges();
    return (fixture.componentInstance as unknown as { previewText: () => string }).previewText();
  };

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [ReasoningContentComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(ReasoningContentComponent);
  });

  afterEach(() => TestBed.resetTestingModule());

  it('follows the latest finished sentence while the model thinks', () => {
    expect(preview('Start with small cases. Then check f(3). Now the gene', { streaming: true }))
      .toBe('Then check f(3).');
  });

  it('keeps a decimal whole rather than splitting at its point', () => {
    expect(preview('The ratio is 2.8405 per column. Compar', { streaming: true }))
      .toBe('The ratio is 2.8405 per column.');
  });

  it('does not take a trailing full stop as finished until more arrives', () => {
    // "2." may be the start of "2.8405".
    expect(preview('First idea. The true value is 2.', { streaming: true })).toBe('First idea.');
  });

  it('shows the opening until any sentence has finished', () => {
    expect(preview('Thinking about tilings', { streaming: true })).toBe('Thinking about tilings');
  });

  it('goes back to the opening once the model has stopped', () => {
    expect(preview('First idea. Second idea. ', { streaming: true, durationMs: 3_000 }))
      .toBe('First idea. Second idea.');
  });

  it('shows the opening on a reloaded block', () => {
    expect(preview('First idea. Second idea. ')).toBe('First idea. Second idea.');
  });
});
