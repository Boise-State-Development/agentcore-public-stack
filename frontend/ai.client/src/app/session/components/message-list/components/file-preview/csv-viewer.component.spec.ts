import { ComponentFixture, TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it } from 'vitest';
import { CsvViewerComponent } from './csv-viewer.component';

function bytes(text: string): ArrayBuffer {
  const encoded = new TextEncoder().encode(text);
  return encoded.buffer.slice(
    encoded.byteOffset,
    encoded.byteOffset + encoded.byteLength,
  ) as ArrayBuffer;
}

describe('CsvViewerComponent', () => {
  let fixture: ComponentFixture<CsvViewerComponent>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [CsvViewerComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(CsvViewerComponent);
  });

  function render(csv: string): HTMLElement {
    fixture.componentRef.setInput('bytes', bytes(csv));
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('renders the header row as column headers', () => {
    const el = render('name,qty\nwidget,3\n');

    const headers = Array.from(el.querySelectorAll('[role="columnheader"]'))
      .map((n) => n.textContent?.trim())
      // The row-number gutter is a columnheader too, labelled for AT.
      .filter((t) => t !== '#');
    expect(headers).toEqual(['name', 'qty']);
  });

  it('reports the true row count to assistive tech, not the rendered window', () => {
    // The whole point of virtualising: only a slice of rows is in the
    // DOM at any time, so aria-rowcount has to come from the data.
    const rows = Array.from({ length: 500 }, (_, i) => `${i},x`).join('\n');
    const el = render(`a,b\n${rows}\n`);

    const grid = el.querySelector('[role="grid"]');
    expect(grid?.getAttribute('aria-rowcount')).toBe('501'); // 500 + header
    expect(grid?.getAttribute('aria-colcount')).toBe('2');
  });

  it('summarises size and delimiter in the footer', () => {
    const el = render('a\tb\n1\t2\n3\t4\n');

    expect(el.querySelector('footer')?.textContent).toContain('2 rows');
    expect(el.querySelector('footer')?.textContent).toContain('2 columns');
    expect(el.querySelector('footer')?.textContent).toContain('tab-separated');
  });

  it('singularises a one-row, one-column file', () => {
    const el = render('name\nwidget\n');

    expect(el.querySelector('footer')?.textContent).toContain('1 row');
    expect(el.querySelector('footer')?.textContent).toContain('1 column');
  });

  it('gives header and body rows one shared track list', () => {
    // Header and rows line up only because they share this custom
    // property — measuring after layout would be a frame too late.
    const el = render('a,b\n1,2\n');

    const grid = el.querySelector('[role="grid"]') as HTMLElement;
    const template = grid.style.getPropertyValue('--grid-cols');
    // Gutter plus one track per column.
    expect(template.trim().split(/\s+/)).toHaveLength(3);
  });

  it('explains a file with headers but no rows', () => {
    const el = render('name,qty\n');

    expect(el.textContent).toContain('no data rows');
    expect(el.querySelector('cdk-virtual-scroll-viewport')).toBeNull();
  });

  it('emits rendered once a grid is on screen', () => {
    const seen: number[] = [];
    fixture.componentInstance.rendered.subscribe(() => seen.push(1));

    render('a,b\n1,2\n');

    expect(seen).toHaveLength(1);
  });

  it('emits renderFailed with the parser message for an empty file', () => {
    const failures: string[] = [];
    fixture.componentInstance.renderFailed.subscribe((m) => failures.push(m));

    render('');

    expect(failures).toEqual(['This file is empty.']);
    expect(fixture.nativeElement.querySelector('[role="grid"]')).toBeNull();
  });

  it('does not emit rendered when the parse failed', () => {
    const seen: number[] = [];
    fixture.componentInstance.rendered.subscribe(() => seen.push(1));

    render('   \n');

    expect(seen).toHaveLength(0);
  });

  it('clears the grid when the bytes go away', () => {
    render('a,b\n1,2\n');
    expect(fixture.nativeElement.querySelector('[role="grid"]')).not.toBeNull();

    fixture.componentRef.setInput('bytes', null);
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('[role="grid"]')).toBeNull();
  });

  it('warns when a cap cut the preview short', () => {
    const wide = Array.from({ length: 300 }, (_, i) => String(i)).join(',');
    const el = render(`${wide}\n${wide}\n`);

    expect(el.querySelector('footer')?.textContent).toContain(
      'later columns not shown',
    );
  });
});
