import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { provideMarkdown, MarkdownService } from 'ngx-markdown';
import { StreamingTextComponent } from './streaming-text.component';

describe('StreamingTextComponent', () => {
  let fixture: ComponentFixture<StreamingTextComponent>;
  let component: StreamingTextComponent;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [StreamingTextComponent],
      providers: [provideMarkdown()],
    }).compileComponents();

    // Stub render before component creation to prevent unhandled
    // rejections from the real KaTeX dependency not being available.
    const markdownService = TestBed.inject(MarkdownService);
    markdownService.render = () => Promise.resolve();

    fixture = TestBed.createComponent(StreamingTextComponent);
    component = fixture.componentInstance;
  });

  it('shows already-streamed text immediately on mount mid-stream (no replay)', () => {
    // Regression: navigating away from a streaming conversation and back
    // recreates the message components. The accumulated partial response
    // must appear instantly — not re-typed from character zero.
    const partial = 'The morning mist clung to the Paraná wetlands like a thin veil.';
    fixture.componentRef.setInput('text', partial);
    fixture.componentRef.setInput('isStreaming', true);
    fixture.detectChanges();

    expect(component.displayedText()).toBe(partial);
  });

  it('shows full text immediately for a non-streaming message', () => {
    fixture.componentRef.setInput('text', 'A completed answer.');
    fixture.componentRef.setInput('isStreaming', false);
    fixture.detectChanges();

    expect(component.displayedText()).toBe('A completed answer.');
  });

  it('animates only text that arrives while mounted', () => {
    fixture.componentRef.setInput('text', 'Seeded prefix. ');
    fixture.componentRef.setInput('isStreaming', true);
    fixture.detectChanges();
    expect(component.displayedText()).toBe('Seeded prefix. ');

    // New delta arrives while mounted — the typewriter picks it up from the
    // seeded position, so the display never regresses below the prefix.
    fixture.componentRef.setInput('text', 'Seeded prefix. And a new sentence.');
    fixture.detectChanges();

    expect(component.displayedText().startsWith('Seeded prefix. ')).toBe(true);
  });

  it('flushes the full text the moment streaming ends', () => {
    fixture.componentRef.setInput('text', 'Partial answer');
    fixture.componentRef.setInput('isStreaming', true);
    fixture.detectChanges();

    fixture.componentRef.setInput('text', 'Partial answer, now complete.');
    fixture.componentRef.setInput('isStreaming', false);
    fixture.detectChanges();

    expect(component.displayedText()).toBe('Partial answer, now complete.');
  });
});
