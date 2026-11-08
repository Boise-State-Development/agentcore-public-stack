import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { KnowledgeModeLineComponent } from './knowledge-mode-line.component';
import {
  KNOWLEDGE_MODE_UNKNOWN,
  KnowledgeModeService,
  KnowledgeModeStatus,
} from './knowledge-mode.service';
import { ConfigService } from '../services/config.service';

function status(overrides: Partial<KnowledgeModeStatus> = {}): KnowledgeModeStatus {
  return { ...KNOWLEDGE_MODE_UNKNOWN, budgetTokens: 50_000, ...overrides };
}

describe('KnowledgeModeService', () => {
  let service: KnowledgeModeService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => 'http://api.test' } },
      ],
    });
    service = TestBed.inject(KnowledgeModeService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => TestBed.resetTestingModule());

  it('reads the mode endpoint', async () => {
    const pending = service.getMode('ast-1');
    const request = http.expectOne('http://api.test/assistants/ast-1/knowledge-base/mode');
    expect(request.request.method).toBe('GET');
    request.flush({ mode: 'whole', documentCount: 2, estimatedTokens: 1200, budgetTokens: 50000 });
    const result = await pending;
    expect(result.mode).toBe('whole');
    expect(result.skippedDocuments).toEqual([]);
  });

  it('fails soft to unknown, which renders nothing', async () => {
    const pending = service.getMode('ast-1');
    http
      .expectOne('http://api.test/assistants/ast-1/knowledge-base/mode')
      .flush('nope', { status: 403, statusText: 'Forbidden' });
    expect((await pending).mode).toBe('unknown');
  });
});

describe('KnowledgeModeLineComponent', () => {
  let fixture: ComponentFixture<KnowledgeModeLineComponent>;
  let getMode: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    getMode = vi.fn().mockResolvedValue(status());
    await TestBed.configureTestingModule({
      imports: [KnowledgeModeLineComponent],
      providers: [{ provide: KnowledgeModeService, useValue: { getMode } }],
    }).compileComponents();
    fixture = TestBed.createComponent(KnowledgeModeLineComponent);
  });

  afterEach(() => TestBed.resetTestingModule());

  async function render(current: KnowledgeModeStatus, entityId: string | null = 'ast-1'): Promise<string> {
    getMode.mockResolvedValue(current);
    fixture.componentRef.setInput('entityId', entityId);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return ((fixture.nativeElement as HTMLElement).textContent ?? '').replace(/\s+/g, ' ').trim();
  }

  it('says when the knowledge base is sent whole, with its size', async () => {
    const text = await render(status({ mode: 'whole', documentCount: 3, estimatedTokens: 48_200 }));
    expect(text).toBe('Sent to the model whole (3 documents, ~48K tokens).');
  });

  it('says spreadsheets and decks are searched instead', async () => {
    const text = await render(
      status({ mode: 'whole', documentCount: 1, estimatedTokens: 900, skippedDocuments: ['budget.xlsx'] }),
    );
    expect(text).toContain('(1 document, ~900 tokens).');
    expect(text).toContain('Spreadsheets and presentations (1) are searched instead.');
  });

  it('says why a knowledge base over the budget is searched', async () => {
    const text = await render(status({ mode: 'searched', reason: 'over_budget', estimatedTokens: 410_000 }));
    expect(text).toBe(
      'Searched: the model sees the passages that match each message, because its text ' +
        '(~410K tokens) is over the 50K-token limit for sending it whole.',
    );
  });

  it('says when one document is over the limit on its own, with its size', async () => {
    const text = await render(
      status({ mode: 'searched', reason: 'document_over_budget', estimatedTokens: 131_040, budgetTokens: 75_000 }),
    );
    expect(text).toBe(
      'Searched: the model sees the passages that match each message, because one document is about ' +
        '131K tokens on its own, over the 75K-token limit for sending it whole.',
    );
  });

  it('keeps a document with no readable text as its own reason', async () => {
    const text = await render(status({ mode: 'searched', reason: 'no_text_layer' }));
    expect(text).toBe(
      'Searched: the model sees the passages that match each message, because a document has no ' +
        'readable text (a scan, for example).',
    );
    expect(text).not.toContain('over the limit');
  });

  it.each([
    ['raw_bytes', 'too large to send whole'],
    ['no_text_documents', 'only spreadsheets or presentations'],
    ['full_context_off', 'this deployment does not send knowledge bases whole'],
  ] as const)('words the %s reason', async (reason, fragment) => {
    const text = await render(status({ mode: 'searched', reason }));
    expect(text.startsWith('Searched: the model sees the passages that match each message, because')).toBe(true);
    expect(text).toContain(fragment);
  });

  it('renders nothing for an empty or unreadable knowledge base, or before there is an id', async () => {
    expect(await render(status({ mode: 'empty' }))).toBe('');
    expect(await render(status({ mode: 'unknown' }))).toBe('');
    getMode.mockClear();
    expect(await render(status({ mode: 'whole' }), null)).toBe('');
    expect(getMode).not.toHaveBeenCalled();
  });

  it('re-reads when the documents change', async () => {
    await render(status({ mode: 'searched', reason: 'over_budget' }));
    expect(getMode).toHaveBeenCalledTimes(1);
    getMode.mockResolvedValue(status({ mode: 'whole', documentCount: 1, estimatedTokens: 10 }));
    fixture.componentRef.setInput('refreshKey', 'd1:complete');
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(getMode).toHaveBeenCalledTimes(2);
    expect((fixture.nativeElement as HTMLElement).textContent).toContain('Sent to the model whole');
  });
});
