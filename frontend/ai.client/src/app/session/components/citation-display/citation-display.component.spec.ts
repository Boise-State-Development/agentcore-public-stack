import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { CitationDisplayComponent, citationPage } from './citation-display.component';
import { Citation } from '../../services/models/message.model';
import { FilePreviewStateService } from '../../services/file-preview/file-preview-state.service';

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

  describe('Download button visibility (#111)', () => {
    const one: Citation[] = [
      { assistantId: 'a1', documentId: 'd1', fileName: 'test.pdf', text: 'excerpt' },
    ];

    it('shows the Download source button by default (allowDownload defaults true)', () => {
      fixture.componentRef.setInput('citations', one);
      component.isExpanded.set(true);
      fixture.detectChanges();

      const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
      expect(component.allowDownload()).toBe(true);
      expect(text).toContain('Download source');
    });

    it('hides the Download source button when allowDownload is false, keeping the citation', () => {
      fixture.componentRef.setInput('citations', one);
      fixture.componentRef.setInput('allowDownload', false);
      component.isExpanded.set(true);
      fixture.detectChanges();

      const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
      expect(text).not.toContain('Download source');
      // The citation itself still renders — only the download affordance is withheld.
      expect(text).toContain('test.pdf');
    });
  });
  describe('Web page citations', () => {
    const web: Citation = {
      assistantId: 'a1',
      documentId: 'd1',
      fileName: 'Research Centers.md',
      text: 'excerpt',
      sourceUrl: 'https://www.example.edu/research/centers/',
    };
    const upload: Citation = { assistantId: 'a1', documentId: 'd2', fileName: 'handbook.pdf', text: 'excerpt' };

    function render(citations: Citation[], allowDownload = true): HTMLElement {
      fixture.componentRef.setInput('citations', citations);
      fixture.componentRef.setInput('allowDownload', allowDownload);
      component.isExpanded.set(true);
      fixture.detectChanges();
      return fixture.nativeElement as HTMLElement;
    }

    it('shows the page title and address and links to the page in a new tab', () => {
      const el = render([web]);
      const link = el.querySelector<HTMLAnchorElement>('a[href]');
      expect(link?.href).toBe('https://www.example.edu/research/centers/');
      expect(link?.target).toBe('_blank');
      expect(link?.rel).toBe('noopener noreferrer');
      expect(link?.textContent).toContain('Open page');
      expect(el.querySelector('[data-testid="citation-page-address"]')?.textContent?.trim()).toBe(
        'www.example.edu/research/centers',
      );
      expect(el.textContent).toContain('Research Centers');
      expect(el.textContent).not.toContain('Research Centers.md');
      expect(el.textContent).not.toContain('Download source');
    });

    it('still links to the page when downloads are disabled', () => {
      const el = render([web], false);
      expect(el.querySelector('a[href]')).not.toBeNull();
    });

    it('leaves an uploaded file as it was', () => {
      const el = render([upload]);
      expect(el.querySelector('a[href]')).toBeNull();
      expect(el.querySelector('[data-testid="citation-page-address"]')).toBeNull();
      expect(el.textContent).toContain('handbook.pdf');
      expect(el.textContent).toContain('Download source');
    });

    it('never links to a non-web address', () => {
      expect(citationPage('javascript:alert(1)')).toBeNull();
      expect(citationPage('ftp://example.edu/x')).toBeNull();
      expect(citationPage('not a url')).toBeNull();
      expect(citationPage(undefined)).toBeNull();
      const el = render([{ ...web, sourceUrl: 'javascript:alert(1)' }]);
      expect(el.querySelector('a[href]')).toBeNull();
      expect(el.textContent).toContain('Research Centers.md');
    });

    it('labels a site root by its host', () => {
      expect(citationPage('https://www.example.edu/')?.label).toBe('www.example.edu');
    });
  });
  describe('Excerpt as plain text', () => {
    function excerptOf(citation: Citation): string {
      fixture.componentRef.setInput('citations', [citation]);
      component.isExpanded.set(true);
      fixture.detectChanges();
      return component.items()[0].excerpt;
    }

    it("drops markdown syntax and a heading that repeats the page's title", () => {
      const excerpt = excerptOf({
        assistantId: 'a1',
        documentId: 'd1',
        fileName: 'Home - DRED at Boise State University.md',
        text: '# Home - DRED at Boise State University\n\n## Supporting research\n\n**The Division** leads [this](https://x.edu) effort.',
        sourceUrl: 'https://www.example.edu/research/',
      });
      expect(excerpt).toBe('Supporting research · The Division leads this effort.');
      const shown = (fixture.nativeElement as HTMLElement).textContent ?? '';
      expect(shown).not.toContain('##');
      expect(shown).not.toContain('**');
    });

    it('keeps a leading heading that is not the title', () => {
      expect(
        excerptOf({ assistantId: 'a1', documentId: 'd2', fileName: 'handbook.pdf', text: '# Grading\n\nLate work loses 10%.' }),
      ).toBe('Grading · Late work loses 10%.');
    });

    it('keeps the only block even when it matches the title', () => {
      expect(excerptOf({ assistantId: 'a1', documentId: 'd3', fileName: 'Notes.md', text: '# Notes' })).toBe('Notes');
    });

    it('passes plain text through', () => {
      expect(
        excerptOf({ assistantId: 'a1', documentId: 'd4', fileName: 'h.pdf', text: 'A plain sentence from a PDF.' }),
      ).toBe('A plain sentence from a PDF.');
    });
  });

  describe('View content', () => {
    const web: Citation = {
      assistantId: 'a1',
      documentId: 'd1',
      fileName: 'Research Centers.md',
      text: '# Research Centers',
      sourceUrl: 'https://www.example.edu/research/centers/',
    };

    function render(citations: Citation[], allowDownload = true): HTMLElement {
      fixture.componentRef.setInput('citations', citations);
      fixture.componentRef.setInput('allowDownload', allowDownload);
      component.isExpanded.set(true);
      fixture.detectChanges();
      return fixture.nativeElement as HTMLElement;
    }

    const viewButton = (el: HTMLElement) =>
      el.querySelector<HTMLButtonElement>('[data-testid="citation-view-content"]');

    it('opens a web page citation in the preview pane as a knowledge-base document', () => {
      const state = TestBed.inject(FilePreviewStateService);
      const open = vi.spyOn(state, 'open');
      const el = render([web]);

      const button = viewButton(el);
      expect(button?.textContent).toContain('View content');
      button!.click();

      expect(open).toHaveBeenCalledWith({
        kind: 'kb-document',
        assistantId: 'a1',
        documentId: 'd1',
        filename: 'Research Centers',
        sourceUrl: 'https://www.example.edu/research/centers/',
      });
      expect(component.isExpanded()).toBe(false);
      // Open page stays beside it.
      expect(el.querySelector('a[href]')?.textContent).toContain('Open page');
      open.mockRestore();
    });

    it('is offered for markdown and text uploads, beside Download source', () => {
      const el = render([
        { assistantId: 'a1', documentId: 'd2', fileName: 'notes.MD', text: 'x' },
        { assistantId: 'a1', documentId: 'd3', fileName: 'readme.txt', text: 'x' },
      ]);
      expect(el.querySelectorAll('[data-testid="citation-view-content"]').length).toBe(2);
      expect(el.textContent).toContain('Download source');
    });

    it('is not offered for a type the server cannot show', () => {
      const el = render([{ assistantId: 'a1', documentId: 'd4', fileName: 'handbook.pdf', text: 'x' }]);
      expect(viewButton(el)).toBeNull();
    });

    it('is withheld with downloads (#111), while Open page stays', () => {
      const el = render([web], false);
      expect(viewButton(el)).toBeNull();
      expect(el.querySelector('a[href]')).not.toBeNull();
    });

    it('needs the ids to read the document', () => {
      const el = render([{ ...web, assistantId: '', documentId: '' }]);
      expect(viewButton(el)).toBeNull();
    });
  });
});
