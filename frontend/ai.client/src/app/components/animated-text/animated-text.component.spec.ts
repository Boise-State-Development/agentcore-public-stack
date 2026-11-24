import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { AnimatedTextComponent, AnimatedTextEntry } from './animated-text.component';

function render(text: string, entry: AnimatedTextEntry, speed = 50) {
  const fixture = TestBed.createComponent(AnimatedTextComponent);
  fixture.componentRef.setInput('text', text);
  fixture.componentRef.setInput('entry', entry);
  fixture.componentRef.setInput('speed', speed);
  fixture.detectChanges();
  return fixture;
}

function visibleLetters(el: HTMLElement): string {
  return Array.from(el.querySelectorAll('.word'))
    .map(w => Array.from(w.querySelectorAll('.ch')).map(c => (c.querySelector('.final') ?? c).textContent).join(''))
    .join(' ');
}

describe('AnimatedTextComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('types the text one character at a time by default', () => {
    const fixture = TestBed.createComponent(AnimatedTextComponent);
    fixture.componentRef.setInput('text', 'Hi you');
    fixture.detectChanges();

    expect(fixture.componentInstance.displayedText()).toBe('H');
    vi.advanceTimersByTime(50 * 5);
    fixture.detectChanges();
    expect(fixture.componentInstance.displayedText()).toBe('Hi you');
    expect(fixture.nativeElement.querySelector('.cursor')).not.toBeNull();

    // The caret goes one tick after the last letter.
    vi.advanceTimersByTime(50);
    fixture.detectChanges();
    expect(fixture.componentInstance.isAnimating()).toBe(false);
    expect(fixture.nativeElement.querySelector('.cursor')).toBeNull();
  });

  it('hides the caret when showCursor is false', () => {
    const fixture = TestBed.createComponent(AnimatedTextComponent);
    fixture.componentRef.setInput('text', 'Hello');
    fixture.componentRef.setInput('showCursor', false);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.cursor')).toBeNull();
  });

  it.each<AnimatedTextEntry>(['focus', 'drop', 'flip', 'aurora'])(
    '%s renders every letter in words with a stagger index',
    entry => {
      const fixture = render('How can I help?', entry);
      const el: HTMLElement = fixture.nativeElement;

      expect(el.querySelector('.stage')?.getAttribute('data-entry')).toBe(entry);
      expect(visibleLetters(el)).toBe('How can I help?');
      const letters = Array.from(el.querySelectorAll<HTMLElement>('.ch'));
      expect(letters.map(l => l.style.getPropertyValue('--i'))).toEqual(
        letters.map((_, i) => String(i)),
      );
    },
  );

  it('gives screen readers the whole text once and hides the letters', () => {
    const fixture = render('How can I help?', 'drop');
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.sr-only')?.textContent).toBe('How can I help?');
    expect(el.querySelector('.stage')?.getAttribute('aria-hidden')).toBe('true');
  });

  it('decode shows noise ahead of the lock front and ends with every letter locked', () => {
    const fixture = render('Hello world', 'decode', 20);
    const el: HTMLElement = fixture.nativeElement;

    vi.advanceTimersByTime(20 * 3);
    fixture.detectChanges();
    expect(el.querySelectorAll('.glyph').length).toBeGreaterThan(0);
    expect(el.querySelectorAll('.locked').length).toBeLessThan(10);

    vi.advanceTimersByTime(20 * 30);
    fixture.detectChanges();
    expect(el.querySelectorAll('.locked').length).toBe(10);
    expect(el.querySelectorAll('.glyph').length).toBe(0);
    expect(visibleLetters(el)).toBe('Hello world');
  });

  it('caps the stagger so a long line still lands in about a second', () => {
    const fixture = render('x'.repeat(200), 'focus', 50);
    const stage = fixture.nativeElement.querySelector('.stage') as HTMLElement;
    expect(stage.style.getPropertyValue('--step')).toBe('12ms');

    fixture.componentRef.setInput('text', 'Short');
    fixture.detectChanges();
    expect(stage.style.getPropertyValue('--step')).toBe('50ms');
  });

  it('re-creates the letters when the text changes so the animation replays', () => {
    const fixture = render('First', 'flip');
    const before = fixture.nativeElement.querySelector('.ch');
    fixture.componentRef.setInput('text', 'Second');
    fixture.detectChanges();
    const after = fixture.nativeElement.querySelector('.ch');
    expect(after).not.toBe(before);
    expect(visibleLetters(fixture.nativeElement)).toBe('Second');
  });
});
