import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { AdminAgentTemplatesService } from './admin-agent-templates.service';
import { ConfigService } from '../../../services/config.service';
import { AgentTemplateCreate } from '../models/agent-template-admin.model';

const BASE = 'http://localhost:8000/admin/agent-templates';

function sampleCreate(): AgentTemplateCreate {
  return {
    name: 'Document Q&A',
    emoji: '📚',
    pitch: 'Answers from your docs',
    description: 'A document-grounded assistant',
    instructions: 'Answer only from the provided material.',
    status: 'enabled',
    sort_order: 0,
    tags: ['qa'],
    starters: ['What can you help with?'],
    modelConfig: { modelId: null, params: {} },
    bindings: [],
  };
}

describe('AdminAgentTemplatesService', () => {
  let service: AdminAgentTemplatesService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        AdminAgentTemplatesService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(AdminAgentTemplatesService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true).forEach(req => {
      if (!req.cancelled) req.flush({ templates: [], total: 0 });
    });
    TestBed.resetTestingModule();
  });

  it('fetches all templates from the list endpoint (trailing slash)', async () => {
    const mockResponse = { templates: [], total: 0 };
    const promise = service.fetchAll();
    await vi.waitFor(() => {
      httpMock.expectOne(`${BASE}/`).flush(mockResponse);
    });
    const result = await promise;
    expect(result).toEqual(mockResponse);
  });

  it('gets one template by id', async () => {
    const promise = service.getTemplate('course-helper');
    const req = httpMock.expectOne(`${BASE}/course-helper`);
    expect(req.request.method).toBe('GET');
    req.flush({ template_id: 'course-helper' });
    await promise;
  });

  it('creates a template via POST to the collection', async () => {
    const promise = service.createTemplate(sampleCreate());
    const req = httpMock.expectOne(`${BASE}/`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body.name).toBe('Document Q&A');
    expect(req.request.body.modelConfig).toEqual({ modelId: null, params: {} });
    req.flush({ template_id: 'document-q-a' });
    await promise;
  });

  it('updates a template via PATCH to the item', async () => {
    const promise = service.updateTemplate('course-helper', { status: 'disabled' });
    const req = httpMock.expectOne(`${BASE}/course-helper`);
    expect(req.request.method).toBe('PATCH');
    expect(req.request.body).toEqual({ status: 'disabled' });
    req.flush({ template_id: 'course-helper', status: 'disabled' });
    await promise;
  });

  it('deletes a template via DELETE to the item', async () => {
    const promise = service.deleteTemplate('course-helper');
    const req = httpMock.expectOne(`${BASE}/course-helper`);
    expect(req.request.method).toBe('DELETE');
    req.flush(null);
    await promise;
  });
});
