import { ComponentFixture, TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { XlsxViewerComponent } from './xlsx-viewer.component';
import {
  FilePreviewError,
  FilePreviewHttpService,
} from '../../../../services/file-preview/file-preview-http.service';
import {
  SheetPreview,
  SheetPreviewResponse,
} from '../../../../services/file-preview/sheet-preview.model';

function sheet(overrides: Partial<SheetPreview> = {}): SheetPreview {
  return {
    name: 'Sheet1',
    headers: ['Item', 'Cost'],
    rows: [['Rent', '1200']],
    totalRows: 1,
    truncated: false,
    truncatedBy: null,
    ...overrides,
  };
}

function response(sheets: SheetPreview[]): SheetPreviewResponse {
  return {
    uploadId: 'up1',
    filename: 'budget.xlsx',
    sheets,
    truncated: sheets.some((s) => s.truncated),
  };
}

describe('XlsxViewerComponent', () => {
  let fixture: ComponentFixture<XlsxViewerComponent>;
  let fetchSheets: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    fetchSheets = vi.fn().mockResolvedValue(response([sheet()]));

    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [XlsxViewerComponent],
      providers: [
        { provide: FilePreviewHttpService, useValue: { fetchSheets } },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(XlsxViewerComponent);
  });

  /** Set the id and drain the fetch promise. */
  async function open(uploadId = 'up1'): Promise<HTMLElement> {
    fixture.componentRef.setInput('uploadId', uploadId);
    fixture.detectChanges();
    for (let i = 0; i < 10; i++) await Promise.resolve();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('asks the server for rows rather than fetching the workbook', async () => {
    await open();

    expect(fetchSheets).toHaveBeenCalledExactlyOnceWith('up1');
  });

  it('renders the sheet it was given', async () => {
    // Asserted through the header row and the grid's own row count
    // rather than cell text: the CDK viewport has zero height under
    // jsdom, so no body rows are ever in the DOM. What matters here is
    // that the sheet reached the grid intact.
    const el = await open();

    const headers = Array.from(el.querySelectorAll('[role="columnheader"]'))
      .map((n) => n.textContent?.trim())
      .filter((t) => t !== '#');
    expect(headers).toEqual(['Item', 'Cost']);
    expect(el.querySelector('[role="grid"]')?.getAttribute('aria-rowcount')).toBe(
      '2',
    );
  });

  it('emits rendered once the grid is on screen', async () => {
    const seen: number[] = [];
    fixture.componentInstance.rendered.subscribe(() => seen.push(1));

    await open();

    expect(seen).toHaveLength(1);
  });

  describe('multiple sheets', () => {
    beforeEach(() => {
      // The sheets differ in their HEADERS, not just their cells, so a
      // tab switch is observable in the DOM that jsdom actually renders.
      fetchSheets.mockResolvedValue(
        response([
          sheet({ name: 'Q1', headers: ['Item', 'Q1 Cost'] }),
          sheet({ name: 'Q2', headers: ['Item', 'Q2 Cost'] }),
        ]),
      );
    });

    it('offers a tab per sheet and opens on the first', async () => {
      const el = await open();

      const tabs = Array.from(el.querySelectorAll('[role="tab"]'));
      expect(tabs.map((t) => t.textContent?.trim())).toEqual(['Q1', 'Q2']);
      expect(tabs[0].getAttribute('aria-selected')).toBe('true');
      expect(el.textContent).toContain('Q1 Cost');
    });

    it('switches the grid when another tab is chosen', async () => {
      const el = await open();

      const tabs = el.querySelectorAll('[role="tab"]');
      (tabs[1] as HTMLElement).click();
      fixture.detectChanges();

      expect(el.textContent).toContain('Q2 Cost');
      expect(el.textContent).not.toContain('Q1 Cost');
      expect(tabs[1].getAttribute('aria-selected')).toBe('true');
    });
  });

  it('shows no tab strip for a single-sheet workbook', async () => {
    const el = await open();

    expect(el.querySelector('[role="tablist"]')).toBeNull();
  });

  describe('the footer summary', () => {
    it('names both counts when the sheet was cut short', async () => {
      // "500 of 12,480 rows" is the honest form — the grid holds 500 but
      // the sheet has far more.
      fetchSheets.mockResolvedValue(
        response([
          sheet({
            rows: [['a', 'b']],
            totalRows: 12480,
            truncated: true,
            truncatedBy: 'rows',
          }),
        ]),
      );

      const el = await open();

      expect(el.querySelector('footer')?.textContent).toContain('1 of 12,480 rows');
      expect(el.querySelector('footer')?.textContent).toContain(
        'later rows not shown',
      );
    });

    it('collapses to one count when nothing was cut', async () => {
      const el = await open();

      const footer = el.querySelector('footer')?.textContent ?? '';
      expect(footer).toContain('1 row');
      expect(footer).not.toContain(' of ');
    });

    it('says values only, because that is what was read', async () => {
      const el = await open();

      expect(el.querySelector('footer')?.textContent).toContain('values only');
    });
  });

  describe('failures', () => {
    it('reports a workbook with no visible sheets rather than a blank grid', async () => {
      fetchSheets.mockResolvedValue(response([]));
      const failures: string[] = [];
      fixture.componentInstance.renderFailed.subscribe((m) => failures.push(m));

      await open();

      expect(failures).toEqual(['This workbook has no visible sheets to show.']);
    });

    it('surfaces the message the http service chose', async () => {
      fetchSheets.mockRejectedValue(
        new FilePreviewError(
          'This workbook is too large to preview. Download it to open in Excel.',
          false,
        ),
      );
      const failures: string[] = [];
      fixture.componentInstance.renderFailed.subscribe((m) => failures.push(m));

      await open();

      expect(failures).toEqual([
        'This workbook is too large to preview. Download it to open in Excel.',
      ]);
    });

    it('does not emit rendered when the read failed', async () => {
      fetchSheets.mockRejectedValue(new FilePreviewError('nope', false));
      const seen: number[] = [];
      fixture.componentInstance.rendered.subscribe(() => seen.push(1));

      await open();

      expect(seen).toHaveLength(0);
    });
  });

  it('discards a response for a file the pane has moved off', async () => {
    // A slow first workbook must not paint over the second one.
    let resolveFirst: (r: SheetPreviewResponse) => void = () => undefined;
    fetchSheets.mockImplementationOnce(
      () => new Promise<SheetPreviewResponse>((r) => (resolveFirst = r)),
    );
    fetchSheets.mockResolvedValueOnce(
      response([sheet({ headers: ['Second'] })]),
    );

    fixture.componentRef.setInput('uploadId', 'up1');
    fixture.detectChanges();
    await open('up2');

    resolveFirst(response([sheet({ headers: ['First'] })]));
    for (let i = 0; i < 10; i++) await Promise.resolve();
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Second');
    expect(fixture.nativeElement.textContent).not.toContain('First');
  });
});
