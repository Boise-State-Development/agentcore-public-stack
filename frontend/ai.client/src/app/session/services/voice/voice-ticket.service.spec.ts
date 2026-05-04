import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { signal } from '@angular/core';

import { VoiceTicketService } from './voice-ticket.service';
import { ConfigService } from '../../../services/config.service';

describe('VoiceTicketService', () => {
  let service: VoiceTicketService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(VoiceTicketService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
    TestBed.resetTestingModule();
  });

  it('POSTs the session_id to /voice/ticket and returns the response', async () => {
    const promise = service.issue('sess-A');
    const req = httpMock.expectOne('http://localhost:8000/voice/ticket');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ session_id: 'sess-A' });
    req.flush({ ticket: 't.k', expires_in: 60 });
    await expect(promise).resolves.toEqual({ ticket: 't.k', expires_in: 60 });
  });

  it('propagates HTTP errors', async () => {
    const promise = service.issue('sess-B');
    const req = httpMock.expectOne('http://localhost:8000/voice/ticket');
    req.flush(
      { detail: 'CSRF token missing or invalid.' },
      { status: 403, statusText: 'Forbidden' },
    );
    await expect(promise).rejects.toBeDefined();
  });
});
