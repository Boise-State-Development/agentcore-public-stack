import { describe, it, expect, beforeEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ArtifactPreviewComponent } from './artifact-preview.component';
import { csvCells, csvTable, isNumeric, markdownBlocks, plainInline, previewKind, svgDataUri } from './artifact-preview';

describe('artifact preview reading', () => {
  it('picks a drawing by content type', () => {
    expect(previewKind('text/markdown; charset=utf-8')).toBe('markdown');
    expect(previewKind('text/csv')).toBe('csv');
    expect(previewKind('TEXT/HTML')).toBe('page');
    expect(previewKind('image/svg+xml')).toBe('svg');
    expect(previewKind('application/json')).toBe('text');
  });

  it('reads Markdown inline syntax as its words', () => {
    expect(plainInline('**Bold** and _em_ with `code`, a [link](https://x.test) ![img](a.png)')).toBe(
      'Bold and em with code, a link',
    );
    expect(plainInline('snake_case_name stays')).toBe('snake_case_name stays');
  });

  it('leaves no tag start behind, closed or not', () => {
    expect(plainInline('a <script src=x')).toBe('a script src=x');
    expect(plainInline('x <<b>script>alert(1)')).not.toContain('<');
    expect(plainInline('<em>kept</em> words')).toBe('kept words');
  });

  it('splits Markdown into headings, items and paragraphs', () => {
    const source = [
      '# Week 7 digest',
      '',
      'Sections 001–005,',
      'posted this week.',
      '---',
      '- [x] Prophase **vs** prometaphase',
      '2. Lab timing',
      '```python',
      '| Section | Seats |',
      '|---|---|',
      '| 001 | 24 |',
      '> quoted',
    ].join('\n');
    expect(markdownBlocks(source)).toEqual([
      { kind: 'heading', level: 1, text: 'Week 7 digest' },
      { kind: 'para', text: 'Sections 001–005, posted this week.' },
      { kind: 'item', text: 'Prophase vs prometaphase' },
      { kind: 'item', text: 'Lab timing' },
      { kind: 'para', text: 'Section · Seats' },
      { kind: 'para', text: '001 · 24 quoted' },
    ]);
  });

  it('reads quoted CSV cells', () => {
    expect(csvCells('a,"b, c","say ""hi""", d ')).toEqual(['a', 'b, c', 'say "hi"', 'd']);
  });

  it('keeps a CSV header and the first rows and columns', () => {
    const source = ['h1,h2,h3,h4,h5', ...Array.from({ length: 9 }, (_, i) => `${i},1,2,3,4`)].join('\r\n');
    const { header, rows } = csvTable(source);
    expect(header).toEqual(['h1', 'h2', 'h3', 'h4']);
    expect(rows.length).toBe(6);
    expect(rows[0]).toEqual(['0', '1', '2', '3']);
  });

  it('knows a figure from a word', () => {
    expect(['24', '-3.5', '$1,200', '45%'].every(isNumeric)).toBe(true);
    expect(['MWF 9:00', '', 'v2'].some(isNumeric)).toBe(false);
  });

  it('encodes an SVG as an image source', () => {
    expect(svgDataUri('<svg a="#"/>')).toBe('data:image/svg+xml;charset=utf-8,%3Csvg%20a%3D%22%23%22%2F%3E');
  });
});

describe('ArtifactPreviewComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ imports: [ArtifactPreviewComponent] });
  });

  function render(contentType: string, excerpt: string | null) {
    const fixture = TestBed.createComponent(ArtifactPreviewComponent);
    fixture.componentRef.setInput('contentType', contentType);
    fixture.componentRef.setInput('excerpt', excerpt);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('is hidden from assistive technology', () => {
    expect(render('text/markdown', '# Hi').getAttribute('aria-hidden')).toBe('true');
  });

  it('draws Markdown as text, never as markup', () => {
    const el = render('text/markdown', '# Title\n\n<img src=x onerror=alert(1)> body');
    const md = el.querySelector('[data-testid="preview-markdown"]') as HTMLElement;
    expect(md.querySelector('img')).toBeNull();
    expect(md.textContent).toContain('Title');
    expect(md.textContent).toContain('body');
  });

  it('draws a CSV as a table with figures aligned right', () => {
    const el = render('text/csv', 'section,seats\n001,24\n002,22');
    const cells = Array.from(el.querySelectorAll('td'));
    expect(cells.map(c => c.textContent)).toEqual(['001', '24', '002', '22']);
    expect(cells[1].classList).toContain('text-right');
  });

  it('draws a page as its text in a browser frame', () => {
    const el = render('text/html', 'Mitosis quiz <b>not bold</b>');
    expect(el.querySelector('[data-testid="preview-page"] p')?.textContent).toBe('Mitosis quiz <b>not bold</b>');
  });

  it('draws an SVG only as an image', () => {
    const el = render('image/svg+xml', '<svg><script>alert(1)</script></svg>');
    expect(el.querySelector('script')).toBeNull();
    expect(el.querySelector('[data-testid="preview-svg"]')?.getAttribute('src')).toMatch(/^data:image\/svg\+xml/);
  });

  it('shows the type icon when there is no excerpt', () => {
    const el = render('text/csv', null);
    expect(el.querySelector('ng-icon')).not.toBeNull();
    expect(el.querySelector('table')).toBeNull();
  });
});
