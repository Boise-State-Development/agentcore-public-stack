// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';

import { AgentTemplatesService, TemplateCatalogResponse } from './agent-templates.service';
import { ConfigService } from '../../services/config.service';
import { TemplateCatalogEntry } from '../agent-form/agent-templates';

/**
 * The service is the whole seam between the picker and the live backend. What matters:
 * it hits the public `/templates/` route, hands back the catalog entries the picker
 * consumes, treats an empty catalog as a normal (non-error) result, and surfaces a
 * transport failure as a rejection so the picker can show its error state.
 */
describe('AgentTemplatesService', () => {
  let service: AgentTemplatesService;
  let httpMock: HttpTestingController;

  const API = 'https://api.test';
  const URL = `${API}/templates/`;

  const ENTRY: TemplateCatalogEntry = {
    draft: {
      templateId: 'document-qa',
      name: 'Document Q&A',
      description: 'Answers from your documents.',
      emoji: '📚',
      instructions: 'You answer strictly from the provided material.',
      tags: [],
      starters: ['What does the material say?'],
      modelConfig: { modelId: null, params: {} },
      bindings: [],
    },
    pitch: 'Answers strictly from the documents you give it.',
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
      ],
    });
    service = TestBed.inject(AgentTemplatesService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('GETs the public /templates/ route and returns the catalog entries', async () => {
    const promise = service.loadTemplates();

    const req = httpMock.expectOne(URL);
    expect(req.request.method).toBe('GET');
    const body: TemplateCatalogResponse = { templates: [ENTRY], total: 1 };
    req.flush(body);

    const result = await promise;
    expect(result).toEqual([ENTRY]);
    // The picker derives both halves from an entry.
    expect(result[0].draft.templateId).toBe('document-qa');
    expect(result[0].pitch.length).toBeGreaterThan(0);
  });

  it('returns [] when the catalog is empty (backend never errors on empty)', async () => {
    const promise = service.loadTemplates();

    httpMock.expectOne(URL).flush({ templates: [], total: 0 } as TemplateCatalogResponse);

    await expect(promise).resolves.toEqual([]);
  });

  it('rejects when the request fails so the picker can show an error state', async () => {
    const promise = service.loadTemplates();

    httpMock
      .expectOne(URL)
      .flush('boom', { status: 500, statusText: 'Server Error' });

    await expect(promise).rejects.toBeTruthy();
  });
});
