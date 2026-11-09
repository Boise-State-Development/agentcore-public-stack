import { describe, it, expect, vi, beforeEach } from 'vitest';
import { TestBed } from '@angular/core/testing';

import {
  processStreamEvent,
  validateDocumentExtractionEvent,
  normalizeDocumentExtractionRun,
  StreamParserCallbacks,
} from './stream-parser-core';
import { ToolInsightService } from '../../../session/services/chat/tool-insight.service';

function field(overrides: Record<string, unknown> = {}) {
  return {
    name: 'budget_cap',
    title: 'Budget cap',
    type: 'currency',
    value: '$500,000',
    state: 'supported',
    method: 'numeric',
    quote: 'Awards will not exceed $500,000 in total costs.',
    locator: { kind: 'line', value: 88, precision: 'measured' },
    ...overrides,
  };
}

function event(overrides: Record<string, unknown> = {}) {
  return {
    type: 'document_extraction',
    sessionId: 'conv-1',
    toolUseId: 'tu-1',
    fieldSet: 'solicitation',
    fieldSetVersion: '2',
    uploadId: 'up-1',
    filename: 'nsf-23-603.html',
    modelId: 'us.anthropic.claude-haiku-4-5-20251001-v1:0',
    schemaEnforced: true,
    artifactId: 'art-1',
    downloadUploadId: 'up-xlsx',
    documentCount: 1,
    fields: [field()],
    ...overrides,
  };
}

describe('validateDocumentExtractionEvent', () => {
  it('accepts a well-formed run', () => {
    expect(validateDocumentExtractionEvent(event())).toBe(true);
  });

  it.each([
    ['a missing tool use id', { toolUseId: undefined }],
    ['an empty tool use id', { toolUseId: '' }],
    ['no fields array', { fields: undefined }],
    ['only invalid fields', { fields: [field({ state: 'verified' })] }],
    ['the wrong type', { type: 'source_provenance' }],
  ])('rejects %s', (_label, overrides) => {
    expect(validateDocumentExtractionEvent(event(overrides))).toBe(false);
  });

  it('drops a field with an unknown state rather than rendering it', () => {
    const data = event({
      fields: [field(), field({ name: 'deadline', state: 'probably_fine' })],
    });
    expect(validateDocumentExtractionEvent(data)).toBe(true);
    expect(data.fields.map((f: { name: string }) => f.name)).toEqual(['budget_cap']);
  });

  it('drops a partial locator to null instead of faking a position', () => {
    const run = normalizeDocumentExtractionRun(
      event({ fields: [field({ locator: { kind: 'page', value: 3 } })] }),
    );
    expect(run?.fields[0].locator).toBeNull();
  });

  it('reads anything but an explicit true as unenforced', () => {
    expect(normalizeDocumentExtractionRun(event({ schemaEnforced: false }))?.schemaEnforced).toBe(false);
    expect(normalizeDocumentExtractionRun(event({ schemaEnforced: 'false' }))?.schemaEnforced).toBe(false);
    expect(normalizeDocumentExtractionRun(event({ schemaEnforced: undefined }))?.schemaEnforced).toBe(true);
  });

  it('keeps an empty value and turns an empty quote into null', () => {
    const run = normalizeDocumentExtractionRun(
      event({ fields: [field({ value: '', quote: '', state: 'unassessed', method: 'empty_value' })] }),
    );
    expect(run?.fields[0].value).toBe('');
    expect(run?.fields[0].quote).toBeNull();
  });

  it('falls back to the field name when a title is missing', () => {
    const run = normalizeDocumentExtractionRun(event({ fields: [field({ title: undefined })] }));
    expect(run?.fields[0].title).toBe('budget_cap');
  });

  it('dispatches to onDocumentExtraction', () => {
    const callbacks: StreamParserCallbacks = { onDocumentExtraction: vi.fn() };
    processStreamEvent('document_extraction', event(), callbacks);
    expect(callbacks.onDocumentExtraction).toHaveBeenCalledOnce();
  });

  it('reports a malformed frame instead of dispatching it', () => {
    const callbacks: StreamParserCallbacks = {
      onDocumentExtraction: vi.fn(),
      onParseError: vi.fn(),
    };
    processStreamEvent('document_extraction', event({ fields: [] }), callbacks);
    expect(callbacks.onDocumentExtraction).not.toHaveBeenCalled();
    expect(callbacks.onParseError).toHaveBeenCalledWith(
      'document_extraction: invalid data structure',
    );
  });
});

describe('ToolInsightService extraction runs', () => {
  let insights: ToolInsightService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    insights = TestBed.inject(ToolInsightService);
  });

  const run = (overrides: Record<string, unknown> = {}) =>
    normalizeDocumentExtractionRun(event(overrides))!;

  it('collects N documents of one call under its tool use id, in order', () => {
    insights.recordExtraction('conv-1', run({ uploadId: 'a', filename: 'a.pdf' }));
    insights.recordExtraction('conv-1', run({ uploadId: 'b', filename: 'b.pdf' }));
    expect(insights.extractionsFor('conv-1', 'tu-1')?.map(r => r.uploadId)).toEqual(['a', 'b']);
  });

  it('replaces a re-delivered document rather than showing it twice', () => {
    insights.recordExtraction('conv-1', run({ uploadId: 'a', filename: 'old.pdf' }));
    insights.recordExtraction('conv-1', run({ uploadId: 'a', filename: 'new.pdf' }));
    const runs = insights.extractionsFor('conv-1', 'tu-1');
    expect(runs?.length).toBe(1);
    expect(runs?.[0].filename).toBe('new.pdf');
  });

  it('scopes runs to their conversation', () => {
    insights.recordExtraction('conv-1', run());
    expect(insights.extractionsFor('conv-2', 'tu-1')).toBeUndefined();
  });

  it('seeds from hydration through the same validator', () => {
    insights.seedExtractionsFromHydration('conv-1', [
      { ...event({ uploadId: 'a' }), quotesOmitted: true },
      { toolUseId: 'tu-1', fields: [field({ state: 'nonsense' })] },
      'not a row',
    ]);
    const runs = insights.extractionsFor('conv-1', 'tu-1');
    expect(runs?.length).toBe(1);
    expect(runs?.[0].quotesOmitted).toBe(true);
  });

  it('never lets a stored row clobber the live run on screen', () => {
    insights.recordExtraction('conv-1', run({ uploadId: 'a' }));
    insights.seedExtractionsFromHydration('conv-1', [
      { ...event({ uploadId: 'a', fields: [field({ quote: '' })] }), quotesOmitted: true },
      event({ uploadId: 'b' }),
    ]);
    const runs = insights.extractionsFor('conv-1', 'tu-1')!;
    expect(runs.map(r => r.uploadId)).toEqual(['a', 'b']);
    expect(runs[0].quotesOmitted).toBeUndefined();
    expect(runs[0].fields[0].quote).toContain('$500,000');
  });

  it('keeps a call’s other insights when its runs arrive', () => {
    insights.recordProvenance('conv-1', {
      type: 'source_provenance',
      sessionId: 'conv-1',
      toolUseId: 'tu-1',
      claims: [
        {
          id: 'x',
          state: 'supported',
          method: 'literal',
          quote: 'q',
          locator: null,
          uploadId: 'a',
          filename: 'a.pdf',
        },
      ],
    });
    insights.recordExtraction('conv-1', run());
    expect(insights.provenanceFor('conv-1', 'tu-1')?.length).toBe(1);
    expect(insights.extractionsFor('conv-1', 'tu-1')?.length).toBe(1);
  });
});
