import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { McpAppProxyService } from './mcp-app-proxy.service';
import { ConfigService } from '../../../services/config.service';
import { SessionService as BffSessionService } from '../../../auth/session.service';
import { SessionService } from '../session/session.service';
import { ToolService } from '../../../services/tool/tool.service';
import { ModelService } from '../model/model.service';

describe('McpAppProxyService', () => {
  let svc: McpAppProxyService;
  let httpMock: HttpTestingController;
  let usingDefault = false;

  beforeEach(() => {
    usingDefault = false;
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        McpAppProxyService,
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
    svc = TestBed.inject(McpAppProxyService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('posts the conversation binding + directive and returns the result', async () => {
    const promise = svc.proxyToolCall('tu-1', 'widget_tool', { q: 'x' });

    const req = httpMock.expectOne(
      'http://localhost:8000/mcp-apps/proxy-call',
    );
    expect(req.request.method).toBe('POST');
    expect(req.request.withCredentials).toBe(true);
    expect(req.request.headers.get('X-CSRF-Token')).toBe('t');
    expect(req.request.body).toEqual({
      sessionId: 's1',
      toolUseId: 'tu-1',
      toolName: 'widget_tool',
      arguments: { q: 'x' },
      enabledTools: ['gw_x'],
      modelId: 'm1',
    });

    req.flush({
      toolUseId: 'tu-1',
      result: { content: [{ type: 'text', text: 'ok' }], isError: false },
    });
    await expect(promise).resolves.toEqual({
      content: [{ type: 'text', text: 'ok' }],
      isError: false,
    });
  });

  it('sends modelId null when using the default model', async () => {
    usingDefault = true;
    const promise = svc.proxyToolCall('tu-1', 'widget_tool', {});
    const req = httpMock.expectOne(
      'http://localhost:8000/mcp-apps/proxy-call',
    );
    expect(req.request.body.modelId).toBeNull();
    req.flush({ toolUseId: 'tu-1', result: { content: [], isError: false } });
    await promise;
  });

  it('rejects with the inference error message on a non-2xx', async () => {
    const promise = svc.proxyToolCall('tu-1', 'blocked', {});
    const req = httpMock.expectOne(
      'http://localhost:8000/mcp-apps/proxy-call',
    );
    req.flush(
      { error: 'Tool is not callable from an MCP App' },
      { status: 403, statusText: 'Forbidden' },
    );
    await expect(promise).rejects.toThrow('Tool is not callable from an MCP App');
  });
});
