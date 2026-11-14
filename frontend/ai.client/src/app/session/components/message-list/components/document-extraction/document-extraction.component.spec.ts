import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { DocumentExtractionComponent } from './document-extraction.component';
import { ArtifactStateService } from '../../../../services/artifacts/artifact-state.service';
import type {
  DocumentExtractionField,
  DocumentExtractionRun,
} from '../../../../../shared/utils/stream-parser';

function field(over: Partial<DocumentExtractionField> = {}): DocumentExtractionField {
  return {
    name: 'budget_cap',
    title: 'Budget cap',
    type: 'currency',
    value: '$500,000',
    state: 'supported',
    method: 'numeric',
    quote: 'Awards will not exceed $500,000 in total costs.',
    locator: { kind: 'line', value: 88, precision: 'measured' },
    ...over,
  };
}

function run(over: Partial<DocumentExtractionRun> = {}): DocumentExtractionRun {
  return {
    toolUseId: 'tu-1',
    fieldSet: 'solicitation',
    fieldSetVersion: '2',
    uploadId: 'up-1',
    filename: 'nsf-23-603.html',
    modelId: 'us.anthropic.claude-haiku-4-5-20251001-v1:0',
    schemaEnforced: true,
    artifactId: 'art-1',
    downloadUploadId: null,
    fields: [
      field(),
      field({
        name: 'deadline',
        title: 'Deadline',
        type: 'date',
        value: 'October 18, 2023',
        method: 'date',
        quote: 'Full proposal deadline: October 18, 2023',
        locator: { kind: 'line', value: 12, precision: 'estimated' },
      }),
    ],
    ...over,
  };
}

const openArtifactPanel = vi.fn();

async function render(
  runs: DocumentExtractionRun[],
): Promise<ComponentFixture<DocumentExtractionComponent>> {
  TestBed.resetTestingModule();
  await TestBed.configureTestingModule({
    imports: [DocumentExtractionComponent],
    providers: [
      {
        provide: ArtifactStateService,
        useValue: { get: () => undefined, openArtifactPanel },
      },
    ],
  }).compileComponents();
  const fixture = TestBed.createComponent(DocumentExtractionComponent);
  fixture.componentRef.setInput('runs', runs);
  await fixture.whenStable();
  fixture.detectChanges();
  return fixture;
}

const host = (f: ComponentFixture<DocumentExtractionComponent>) =>
  f.nativeElement as HTMLElement;
const text = (f: ComponentFixture<DocumentExtractionComponent>) =>
  (host(f).textContent ?? '').replace(/\s+/g, ' ');

describe('DocumentExtractionComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    openArtifactPanel.mockReset();
  });

  it('renders nothing without runs', async () => {
    const fixture = await render([]);
    expect(host(fixture).textContent?.trim()).toBe('');
  });

  describe('one document', () => {
    it('opens by default, since the model is told the table is on screen', async () => {
      const fixture = await render([run()]);
      expect(text(fixture)).toContain('Extracted 2 fields from nsf-23-603.html');
      const toggle = host(fixture).querySelector('button')!;
      expect(toggle.getAttribute('aria-expanded')).toBe('true');
    });

    it('lists fields with their value, located source and passage', async () => {
      const fixture = await render([run()]);
      const rows = host(fixture).querySelectorAll('tbody tr');
      expect(rows.length).toBe(2);
      expect(rows[0].textContent).toContain('Budget cap');
      expect(rows[0].textContent).toContain('$500,000');
      expect(rows[0].textContent).toContain('found on line 88');
      expect(rows[0].querySelector('q')?.textContent).toContain('will not exceed');
    });

    it('hedges an estimated position', async () => {
      const fixture = await render([run()]);
      expect(text(fixture)).toContain('found on about line 12');
    });

    it('never heads a column "verified"', async () => {
      const fixture = await render([run()]);
      const headers = [...host(fixture).querySelectorAll('thead th')].map(th =>
        th.textContent?.trim().toLowerCase(),
      );
      expect(headers).toEqual(['field', 'value', 'source']);
      expect(text(fixture)).toContain('not that the passage assigns it to the field');
    });

    it('counts and words a value the passage does not support', async () => {
      const fixture = await render([
        run({
          fields: [field({ state: 'quote_unsupported', method: 'no_match', value: '$750,000' })],
        }),
      ]);
      expect(text(fixture)).toContain('1 unconfirmed');
      expect(text(fixture)).toContain('not in the quoted passage on line 88');
      // No bare "found on" for an unsupported value.
      expect(text(fixture)).not.toContain('found on line 88');
    });

    it('says a field the document does not answer was not found', async () => {
      const fixture = await render([
        run({
          fields: [
            field({ value: '', state: 'unassessed', method: 'empty_value', quote: null, locator: null }),
          ],
        }),
      ]);
      expect(text(fixture)).toContain('Not found');
      expect(text(fixture)).toContain('the document does not answer this field');
    });

    it('distinguishes a value with no passage at all', async () => {
      const fixture = await render([
        run({ fields: [field({ state: 'unverified', method: 'no_quote', quote: null, locator: null })] }),
      ]);
      expect(text(fixture)).toContain('no source passage given');
    });

    it('discloses an unenforced schema', async () => {
      const fixture = await render([run({ schemaEnforced: false })]);
      expect(text(fixture)).toContain('requested but not enforced');
    });

    it('says nothing about enforcement when the schema was enforced', async () => {
      const fixture = await render([run()]);
      expect(text(fixture)).not.toContain('not enforced');
    });

    it('names the extraction model', async () => {
      const fixture = await render([run()]);
      expect(text(fixture)).toContain('Extracted with us.anthropic.claude-haiku-4-5');
    });

    it('opens the full table in the artifact panel', async () => {
      const fixture = await render([run()]);
      const open = [...host(fixture).querySelectorAll('button')].find(b =>
        b.textContent?.includes('Open full table'),
      )!;
      open.click();
      expect(openArtifactPanel).toHaveBeenCalledWith(
        expect.objectContaining({ artifactId: 'art-1', version: 1 }),
      );
    });

    it('notes quotes dropped from the stored row', async () => {
      const fixture = await render([run({ quotesOmitted: true })]);
      expect(text(fixture)).toContain('the full table has them');
    });

    it('hides the table from assistive tech when collapsed', async () => {
      const fixture = await render([run()]);
      host(fixture).querySelector('button')!.click();
      fixture.detectChanges();
      const panel = host(fixture).querySelector('.collapsible-content')!;
      expect(panel.getAttribute('aria-hidden')).toBe('true');
      expect(panel.hasAttribute('inert')).toBe(true);
    });
  });

  describe('several documents', () => {
    const two = () => [
      run(),
      run({
        uploadId: 'up-2',
        filename: 'nih-par-24.pdf',
        fields: [
          field({
            value: '$750,000',
            state: 'quote_unsupported',
            method: 'no_match',
            locator: { kind: 'page', value: 4, precision: 'measured' },
          }),
          field({ name: 'deadline', title: 'Deadline', value: '', state: 'unassessed', method: 'empty_value', quote: null, locator: null }),
        ],
      }),
    ];

    it('lays documents out as rows and fields as columns', async () => {
      const fixture = await render(two());
      expect(text(fixture)).toContain('Extracted 2 fields from 2 documents');
      const headers = [...host(fixture).querySelectorAll('thead th')].map(th => th.textContent?.trim());
      expect(headers).toEqual(['Document', 'Budget cap', 'Deadline']);
      const rowHeads = [...host(fixture).querySelectorAll('tbody th')].map(th => th.textContent?.trim());
      expect(rowHeads).toEqual(['nsf-23-603.html', 'nih-par-24.pdf']);
    });

    it('words every unconfirmed cell, not just colours it', async () => {
      const fixture = await render(two());
      expect(text(fixture)).toContain('not in passage');
      expect(text(fixture)).toContain('1 unconfirmed');
    });

    it('shows the first unsupported passage without a click', async () => {
      const fixture = await render(two());
      const detail = host(fixture).querySelector('.detail')!;
      expect(detail.textContent).toContain('nih-par-24.pdf');
      expect(detail.textContent).toContain('not in the quoted passage on page 4');
    });

    it('shows a selected cell’s passage', async () => {
      const fixture = await render(two());
      const cell = host(fixture).querySelector<HTMLButtonElement>('tbody td button')!;
      cell.click();
      fixture.detectChanges();
      expect(cell.getAttribute('aria-expanded')).toBe('true');
      const detail = host(fixture).querySelector('.detail')!;
      expect(detail.textContent).toContain('nsf-23-603.html');
      expect(detail.textContent).toContain('found on line 88');
    });

    it('labels each cell in full for a screen reader', async () => {
      const fixture = await render(two());
      const labels = [...host(fixture).querySelectorAll('tbody td button')].map(b =>
        b.getAttribute('aria-label'),
      );
      expect(labels).toContain(
        'Budget cap, nih-par-24.pdf: $750,000, not in the quoted passage on page 4',
      );
    });

    it('names only the documents whose schema was not enforced', async () => {
      const runs = two();
      runs[1] = { ...runs[1], schemaEnforced: false };
      const fixture = await render(runs);
      expect(text(fixture)).toContain('not enforced for nih-par-24.pdf');
    });
  });
});
