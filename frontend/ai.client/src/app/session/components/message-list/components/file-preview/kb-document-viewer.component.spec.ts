import { ComponentFixture, TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { KbDocumentViewerComponent, describeFailure } from './kb-document-viewer.component';
import {
  DocumentService,
  DocumentUploadError,
} from '../../../../../assistants/services/document.service';
import { DocumentContentResponse } from '../../../../../assistants/models/document.model';

describe('KbDocumentViewerComponent', () => {
  let fixture: ComponentFixture<KbDocumentViewerComponent>;
  let getDocumentContent: ReturnType<typeof vi.fn>;

  function content(overrides: Partial<DocumentContentResponse> = {}): DocumentContentResponse {
    return {
      filename: 'Home - Example.md',
      sourceUrl: 'https://www.example.edu/',
      contentType: 'text/markdown',
      text: '# Home - Example\n\n## Programs\n\n<script>alert(1)</script>',
      truncated: false,
      ...overrides,
    };
  }

  async function settle(): Promise<void> {
    for (let i = 0; i < 10; i++) await Promise.resolve();
    fixture.detectChanges();
  }

  async function mount(): Promise<HTMLElement> {
    fixture = TestBed.createComponent(KbDocumentViewerComponent);
    fixture.componentRef.setInput('assistantId', 'a1');
    fixture.componentRef.setInput('documentId', 'd1');
    fixture.detectChanges();
    await settle();
    return fixture.nativeElement as HTMLElement;
  }

  beforeEach(async () => {
    getDocumentContent = vi.fn().mockResolvedValue(content());
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [KbDocumentViewerComponent],
      providers: [{ provide: DocumentService, useValue: { getDocumentContent } }],
    }).compileComponents();
  });

  it('reads the document by its ids and renders the markdown', async () => {
    const el = await mount();
    expect(getDocumentContent).toHaveBeenCalledWith('a1', 'd1');
    expect(el.querySelector('h1')?.textContent).toBe('Home - Example');
    expect(el.querySelector('h2')?.textContent).toBe('Programs');
    // Raw HTML in the indexed text stays text.
    expect(el.querySelector('script')).toBeNull();
    expect(el.textContent).toContain('<script>alert(1)</script>');
  });

  it('emits rendered once the text is shown', async () => {
    fixture = TestBed.createComponent(KbDocumentViewerComponent);
    const rendered = vi.fn();
    fixture.componentInstance.rendered.subscribe(rendered);
    fixture.componentRef.setInput('assistantId', 'a1');
    fixture.componentRef.setInput('documentId', 'd1');
    fixture.detectChanges();
    await settle();
    expect(rendered).toHaveBeenCalledTimes(1);
  });

  it('shows plain text as preformatted text', async () => {
    getDocumentContent.mockResolvedValue(content({ contentType: 'text/plain', text: '# not a heading' }));
    const el = await mount();
    expect(el.querySelector('h1')).toBeNull();
    expect(el.querySelector('pre')?.textContent).toBe('# not a heading');
  });

  it('says when only the head of a long document is shown', async () => {
    getDocumentContent.mockResolvedValue(content({ truncated: true }));
    const el = await mount();
    expect(el.querySelector('[role="note"]')?.textContent).toContain('first 1 MB');
  });

  it('says when there is no text', async () => {
    getDocumentContent.mockResolvedValue(content({ text: '  \n' }));
    const el = await mount();
    expect(el.textContent).toContain('no indexed text');
  });

  it('reports a failed read instead of rendering', async () => {
    getDocumentContent.mockRejectedValue(
      new DocumentUploadError('Only web pages and markdown or plain-text documents can be shown here.', 'HTTP_415', {
        status: 415,
      }),
    );
    fixture = TestBed.createComponent(KbDocumentViewerComponent);
    const failed = vi.fn();
    fixture.componentInstance.loadFailed.subscribe(failed);
    fixture.componentRef.setInput('assistantId', 'a1');
    fixture.componentRef.setInput('documentId', 'd1');
    fixture.detectChanges();
    await settle();
    expect(failed).toHaveBeenCalledWith({
      message: 'Only web pages and markdown or plain-text documents can be shown here.',
      retryable: false,
    });
    expect(fixture.nativeElement.querySelector('[data-testid="kb-document-content"]')).toBeNull();
  });
});

describe('describeFailure', () => {
  const err = (status: number, message = 'server words') =>
    new DocumentUploadError(message, `HTTP_${status}`, { status });

  it("uses the server's words for refusals, which retrying cannot fix", () => {
    for (const status of [403, 409, 415]) {
      expect(describeFailure(err(status))).toEqual({ message: 'server words', retryable: false });
    }
  });

  it('names a removed document', () => {
    expect(describeFailure(err(404))).toEqual({
      message: 'This document is no longer in the knowledge base.',
      retryable: false,
    });
  });

  it('offers a retry for anything else', () => {
    expect(describeFailure(err(500)).retryable).toBe(true);
    expect(describeFailure(new Error('network')).retryable).toBe(true);
  });
});
