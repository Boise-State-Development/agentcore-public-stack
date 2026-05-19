import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { McpAppMessageService } from './mcp-app-message.service';
import { ConfigService } from '../../../services/config.service';
import { SessionService as BffSessionService } from '../../../auth/session.service';
import { SessionService } from '../session/session.service';
import { ToolService } from '../../../services/tool/tool.service';
import { ModelService } from '../model/model.service';

describe('McpAppMessageService', () => {
  let svc: McpAppMessageService;
  let httpMock: HttpTestingController;
  let usingDefault = false;

  beforeEach(() => {
    usingDefault = false;
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        McpAppMessageService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
        { provide: BffSessionService, useValue: { csrfHeaders: vi.fn().mockReturnValue({ 'X-CSRF-Token': 't' }) } },
        { provide: SessionService, useValue: { currentSession: signal({ sessionId: 's1' }) } },
        { provide: ToolService, useValue: { getEnabledToolIds: vi.fn().mockReturnValue(['gw_x']) } },
        {
          provide: ModelService,
          useValue: {
            isUsingDefaultModel: () => usingDefault,
            selectedModel: signal({ modelId: 'm1' }),
          },
        },
      ],
    });
    svc = TestBed.inject(McpAppMessageService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('posts the conversation binding + resourceUri + payload', async () => {
    const promise = svc.updateModelContext('ui://srv/w', {
      structuredContent: { picked: 'X' },
    });

    const req = httpMock.expectOne(
      'http://localhost:8000/mcp-apps/update-context',
    );
    expect(req.request.method).toBe('POST');
    expect(req.request.withCredentials).toBe(true);
    expect(req.request.headers.get('X-CSRF-Token')).toBe('t');
    expect(req.request.body).toEqual({
      sessionId: 's1',
      resourceUri: 'ui://srv/w',
      content: null,
      structuredContent: { picked: 'X' },
      enabledTools: ['gw_x'],
      modelId: 'm1',
    });

    req.flush({ resourceUri: 'ui://srv/w', status: 'stored' });
    await expect(promise).resolves.toBeUndefined();
  });

  it('sends modelId null when using the default model', async () => {
    usingDefault = true;
    const promise = svc.updateModelContext('ui://srv/w', {
      content: [{ type: 'text', text: 'note' }],
    });
    const req = httpMock.expectOne(
      'http://localhost:8000/mcp-apps/update-context',
    );
    expect(req.request.body.modelId).toBeNull();
    expect(req.request.body.content).toEqual([{ type: 'text', text: 'note' }]);
    req.flush({ status: 'stored' });
    await promise;
  });

  it('rejects with the inference error message on a non-2xx', async () => {
    const promise = svc.updateModelContext('ui://srv/w', {
      structuredContent: {},
    });
    const req = httpMock.expectOne(
      'http://localhost:8000/mcp-apps/update-context',
    );
    req.flush(
      { error: 'needs content' },
      { status: 400, statusText: 'Bad Request' },
    );
    await expect(promise).rejects.toThrow('needs content');
  });
});
