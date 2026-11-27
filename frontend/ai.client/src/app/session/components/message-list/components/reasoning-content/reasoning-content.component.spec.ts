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

/**
 * Live thinking: while the model thinks, the box opens on its own and follows
 * the text, then closes when the answer starts. A reloaded block never opens.
 */
describe('ReasoningContentComponent — live thinking', () => {
  let fixture: ComponentFixture<ReasoningContentComponent>;

  const SHORT = 'The user says hello. Reply briefly.';
  const LONG =
    'The user wants the number of domino tilings of a 4x7 board. Set up the transfer matrix over column profiles. ';

  const render = (text: string, opts: { streaming?: boolean; durationMs?: number } = {}): HTMLElement => {
    fixture.componentRef.setInput('contentBlock', {
      type: 'reasoningContent',
      reasoningContent: { reasoningText: { text } },
      ...(opts.durationMs === undefined ? {} : { reasoningDurationMs: opts.durationMs }),
    } as ContentBlock);
    fixture.componentRef.setInput('isStreaming', opts.streaming ?? false);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  };

  const isOpen = (host: HTMLElement): boolean =>
    host.querySelector('button')!.getAttribute('aria-expanded') === 'true';

  /** jsdom lays nothing out, so give the box a height its text overflows. */
  const sizeBox = (host: HTMLElement, scrollHeight: number, clientHeight = 192): HTMLElement => {
    const box = host.querySelector<HTMLElement>('.reasoning-scroll')!;
    Object.defineProperty(box, 'scrollHeight', { value: scrollHeight, configurable: true });
    Object.defineProperty(box, 'clientHeight', { value: clientHeight, configurable: true });
    return box;
  };

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [ReasoningContentComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(ReasoningContentComponent);
  });

  afterEach(() => TestBed.resetTestingModule());

  it('opens on its own once the thinking outgrows the one-line preview', () => {
    const host = render(LONG, { streaming: true });
    expect(isOpen(host)).toBe(true);
    expect(host.querySelector('.collapsible-content')!.hasAttribute('inert')).toBe(false);
    expect(host.querySelector('.reasoning-scroll')!.textContent).toContain('transfer matrix');
  });

  it('stays shut for a thought the preview line already shows whole', () => {
    const host = render(SHORT, { streaming: true });
    expect(isOpen(host)).toBe(false);
    expect(host.querySelector('button')!.textContent).toContain('The user says hello.');
  });

  it('closes when the model starts answering', () => {
    render(LONG, { streaming: true });
    const host = render(LONG, { streaming: true, durationMs: 4_000 });
    expect(isOpen(host)).toBe(false);
    expect(host.querySelector('.collapsible-content')!.hasAttribute('inert')).toBe(true);
  });

  it('closes when the turn ends without an answer', () => {
    render(LONG, { streaming: true });
    expect(isOpen(render(LONG, { streaming: false }))).toBe(false);
  });

  it('never opens a reloaded block, however long', () => {
    expect(isOpen(render(LONG.repeat(5)))).toBe(false);
    expect(isOpen(render(LONG.repeat(5), { durationMs: 9_000 }))).toBe(false);
  });

  it('uses the shorter box only while it is live', () => {
    const live = render(LONG, { streaming: true }).querySelector('.reasoning-scroll')!;
    expect(live.classList).toContain('max-h-48');
    const done = render(LONG, { streaming: true, durationMs: 4_000 }).querySelector('.reasoning-scroll')!;
    expect(done.classList).toContain('max-h-80');
    expect(done.classList).not.toContain('max-h-48');
  });

  it('keeps the box shut once the user has closed it mid-thought', () => {
    render(LONG, { streaming: true });
    fixture.componentInstance.toggleExpanded();
    expect(isOpen(render(LONG + LONG, { streaming: true }))).toBe(false);
  });

  it('keeps the box open after thinking when the user opened it', () => {
    render(SHORT, { streaming: true });
    fixture.componentInstance.toggleExpanded();
    expect(isOpen(render(SHORT, { streaming: true, durationMs: 800 }))).toBe(true);
  });

  it('follows the newest text as it streams', () => {
    const box = sizeBox(render(LONG, { streaming: true }), 400);
    render(LONG + LONG, { streaming: true });
    expect(box.scrollTop).toBe(400);
  });

  it('stops following once the reader scrolls back', () => {
    const box = sizeBox(render(LONG, { streaming: true }), 400);
    render(LONG + LONG, { streaming: true });
    box.scrollTop = 40;
    box.dispatchEvent(new Event('scroll'));
    Object.defineProperty(box, 'scrollHeight', { value: 600, configurable: true });
    render(LONG + LONG + LONG, { streaming: true });
    expect(box.scrollTop).toBe(40);
  });

  it('leaves the box open for a reader who scrolled back when the answer starts', () => {
    const box = sizeBox(render(LONG, { streaming: true }), 400);
    box.scrollTop = 40;
    box.dispatchEvent(new Event('scroll'));
    expect(isOpen(render(LONG, { streaming: true, durationMs: 4_000 }))).toBe(true);
  });

  it('leaves the box open for a reader whose focus is in it', () => {
    const host = render(LONG, { streaming: true });
    document.body.appendChild(host);
    host.querySelector<HTMLElement>('.reasoning-scroll')!.focus();
    expect(isOpen(render(LONG, { streaming: true, durationMs: 4_000 }))).toBe(true);
    host.remove();
  });

  it('opens without taking focus', () => {
    const elsewhere = document.createElement('textarea');
    document.body.appendChild(elsewhere);
    elsewhere.focus();
    const host = render(LONG, { streaming: true });
    document.body.appendChild(host);
    render(LONG + LONG, { streaming: true });
    expect(document.activeElement).toBe(elsewhere);
    host.remove();
    elsewhere.remove();
  });

  it('announces that thinking started and how long it took, not the text', () => {
    const status = () => (fixture.nativeElement as HTMLElement).querySelector('[role="status"]')!;
    render(LONG, { streaming: true });
    expect(status().textContent).toBe('Thinking');
    render(LONG, { streaming: true, durationMs: 3_000 });
    expect(status().textContent).toBe('Thought for 3s');
    expect(status().classList).toContain('sr-only');
  });

  it('announces nothing for a reloaded block', () => {
    const host = render(LONG, { durationMs: 3_000 });
    expect(host.querySelector('[role="status"]')!.textContent).toBe('');
  });
});
