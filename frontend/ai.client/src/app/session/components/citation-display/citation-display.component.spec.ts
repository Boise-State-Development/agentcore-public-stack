import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { CitationDisplayComponent } from './citation-display.component';
import { Citation } from '../../services/models/message.model';

describe('CitationDisplayComponent', () => {
  let component: CitationDisplayComponent;
  let fixture: ComponentFixture<CitationDisplayComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [CitationDisplayComponent]
    }).compileComponents();

    fixture = TestBed.createComponent(CitationDisplayComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  // Several tests below install fake timers — some in a nested `beforeEach`,
  // some inline inside an `it`. `vi.restoreAllMocks()` does not restore timers,
  // so reset them here for every test in the file. The builder runs vitest with
  // `isolate: false`; a frozen clock left behind here times out whichever
  // unrelated spec lands next in the same worker.
  afterEach(() => {
    vi.useRealTimers();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should accept citations input', () => {
    const mockCitations: Citation[] = [
      {
        assistantId: 'assistant-1',
        documentId: 'doc-1',
        fileName: 'test.pdf',
        text: 'Test citation text'
      }
    ];

    fixture.componentRef.setInput('citations', mockCitations);
    fixture.detectChanges();

    expect(component.citations()).toEqual(mockCitations);
  });

  it('should handle empty citations array', () => {
    fixture.componentRef.setInput('citations', []);
    fixture.detectChanges();

    expect(component.citations()).toEqual([]);
  });

  describe('Badge Display Logic', () => {
    it('should initialize isExpanded signal to false', () => {
      expect(component.isExpanded()).toBe(false);
    });

    it('should allow setting isExpanded signal', () => {
      component.isExpanded.set(true);
      expect(component.isExpanded()).toBe(true);

      component.isExpanded.set(false);
      expect(component.isExpanded()).toBe(false);
    });

    it('should return correct citation count for empty array', () => {
      fixture.componentRef.setInput('citations', []);
      fixture.detectChanges();

      expect(component.citationCount()).toBe(0);
    });

    it('should return correct citation count for single citation', () => {
      const mockCitations: Citation[] = [
        {
          assistantId: 'assistant-1',
          documentId: 'doc-1',
          fileName: 'test.pdf',
          text: 'Test citation text'
        }
      ];

      fixture.componentRef.setInput('citations', mockCitations);
      fixture.detectChanges();

      expect(component.citationCount()).toBe(1);
    });

    it('should return correct citation count for multiple citations', () => {
      const mockCitations: Citation[] = [
        {
          assistantId: 'assistant-1',
          documentId: 'doc-1',
          fileName: 'test1.pdf',
          text: 'Test citation 1'
        },
        {
          assistantId: 'assistant-1',
          documentId: 'doc-2',
          fileName: 'test2.pdf',
          text: 'Test citation 2'
        },
        {
          assistantId: 'assistant-1',
          documentId: 'doc-3',
          fileName: 'test3.pdf',
          text: 'Test citation 3'
        }
      ];

      fixture.componentRef.setInput('citations', mockCitations);
      fixture.detectChanges();

      expect(component.citationCount()).toBe(3);
    });
  });

  describe('Hover Interaction Logic', () => {
    beforeEach(() => {
    TestBed.resetTestingModule();
      vi.useFakeTimers();
    });

    afterEach(() => {
    TestBed.resetTestingModule();
      vi.useRealTimers();
      vi.restoreAllMocks();
    });

    it('should expand on mouse enter', () => {
      expect(component.isExpanded()).toBe(false);

      component.onMouseEnter();

      expect(component.isExpanded()).toBe(true);
    });

    it('should collapse after 300ms delay on mouse leave', () => {
      component.isExpanded.set(true);
      expect(component.isExpanded()).toBe(true);

      component.onMouseLeave();

      // Should still be expanded immediately after mouse leave
      expect(component.isExpanded()).toBe(true);

      // Advance time by 300ms
      vi.advanceTimersByTime(300);

      // Should now be collapsed
      expect(component.isExpanded()).toBe(false);
    });

    it('should clear collapse timeout on mouse enter', () => {
      component.isExpanded.set(true);

      // Start collapse sequence
      component.onMouseLeave();

      // Mouse enters again before timeout completes
      vi.advanceTimersByTime(150); // Only 150ms passed
      component.onMouseEnter();

      // Advance past the original 300ms
      vi.advanceTimersByTime(200); // Total 350ms

      // Should still be expanded because timeout was cleared
      expect(component.isExpanded()).toBe(true);
    });

    it('should handle multiple mouse enter/leave cycles', () => {
      // First cycle
      component.onMouseEnter();
      expect(component.isExpanded()).toBe(true);

      component.onMouseLeave();
      vi.advanceTimersByTime(300);
      expect(component.isExpanded()).toBe(false);

      // Second cycle
      component.onMouseEnter();
      expect(component.isExpanded()).toBe(true);

      component.onMouseLeave();
      vi.advanceTimersByTime(300);
      expect(component.isExpanded()).toBe(false);
    });

    it('should not collapse if mouse re-enters before timeout', () => {
      component.onMouseEnter();
      expect(component.isExpanded()).toBe(true);

      // Leave and re-enter quickly
      component.onMouseLeave();
      vi.advanceTimersByTime(100);
      component.onMouseEnter();

      // Advance past original timeout
      vi.advanceTimersByTime(250);

      // Should still be expanded
      expect(component.isExpanded()).toBe(true);
    });

    it('should handle mouse enter when already expanded', () => {
      component.isExpanded.set(true);

      component.onMouseEnter();

      expect(component.isExpanded()).toBe(true);
    });

    it('should handle mouse leave when already collapsed', () => {
      expect(component.isExpanded()).toBe(false);

      component.onMouseLeave();
      vi.advanceTimersByTime(300);

      expect(component.isExpanded()).toBe(false);
    });
  });

  describe('Keyboard Navigation', () => {
    it('should collapse on Escape key press when expanded', () => {
      component.isExpanded.set(true);
      expect(component.isExpanded()).toBe(true);

      component.onEscapeKey();

      expect(component.isExpanded()).toBe(false);
    });

    it('should not change state on Escape key when already collapsed', () => {
      expect(component.isExpanded()).toBe(false);

      component.onEscapeKey();

      expect(component.isExpanded()).toBe(false);
    });

    it('should clear collapse timeout on Escape key', () => {
      vi.useFakeTimers();

      component.isExpanded.set(true);
      component.onMouseLeave(); // Start collapse timeout

      // Press Escape before timeout completes
      vi.advanceTimersByTime(100);
      component.onEscapeKey();

      expect(component.isExpanded()).toBe(false);

      // Advance past original timeout
      vi.advanceTimersByTime(250);

      // Should remain collapsed (timeout was cleared)
      expect(component.isExpanded()).toBe(false);

      vi.restoreAllMocks();
    });

    it('should toggle expansion on Enter key press', () => {
      const event = new KeyboardEvent('keydown', { key: 'Enter' });
      const preventDefaultSpy = vi.spyOn(event, 'preventDefault');

      expect(component.isExpanded()).toBe(false);

      component.onBadgeKeydown(event);

      expect(component.isExpanded()).toBe(true);
      expect(preventDefaultSpy).toHaveBeenCalled();

      component.onBadgeKeydown(event);

      expect(component.isExpanded()).toBe(false);
    });

    it('should toggle expansion on Space key press', () => {
      const event = new KeyboardEvent('keydown', { key: ' ' });
      const preventDefaultSpy = vi.spyOn(event, 'preventDefault');

      expect(component.isExpanded()).toBe(false);

      component.onBadgeKeydown(event);

      expect(component.isExpanded()).toBe(true);
      expect(preventDefaultSpy).toHaveBeenCalled();
    });

    it('should not toggle on other key presses', () => {
      const event = new KeyboardEvent('keydown', { key: 'a' });
      const preventDefaultSpy = vi.spyOn(event, 'preventDefault');

      expect(component.isExpanded()).toBe(false);

      component.onBadgeKeydown(event);

      expect(component.isExpanded()).toBe(false);
      expect(preventDefaultSpy).not.toHaveBeenCalled();
    });

    it('should clear collapse timeout on badge keydown', () => {
      vi.useFakeTimers();

      component.isExpanded.set(true);
      component.onMouseLeave(); // Start collapse timeout

      // Press Enter before timeout completes
      vi.advanceTimersByTime(100);
      const event = new KeyboardEvent('keydown', { key: 'Enter' });
      component.onBadgeKeydown(event);

      // Should toggle to collapsed
      expect(component.isExpanded()).toBe(false);

      // Advance past original timeout
      vi.advanceTimersByTime(250);

      // Should remain collapsed (timeout was cleared)
      expect(component.isExpanded()).toBe(false);

      vi.restoreAllMocks();
    });
  });
});
