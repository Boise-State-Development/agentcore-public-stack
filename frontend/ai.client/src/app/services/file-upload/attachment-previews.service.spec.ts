import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';

import { ConfigService } from '../config.service';
import { AttachmentPreviewsService } from './attachment-previews.service';

describe('AttachmentPreviewsService', () => {
  let service: AttachmentPreviewsService;
  let http: HttpTestingController;
  const doc = { version: 1 as const, kind: 'doc' as const, lines: ['Memo'] };

  beforeEach(() => {
    vi.useFakeTimers();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(AttachmentPreviewsService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
    vi.useRealTimers();
  });

  it('sends the cards that ask together as one request', async () => {
    const a = service.preview('a');
    const b = service.preview('b');
    await vi.advanceTimersByTimeAsync(30);
    const request = http.expectOne((r) => r.url === 'http://localhost:8000/files/previews');
    expect(request.request.params.get('ids')).toBe('a,b');
    request.flush({ previews: { a: doc, b: null } });
    expect(await a).toEqual(doc);
    expect(await b).toBeNull();
  });

  it('answers a second ask for the same file from its cache', async () => {
    const first = service.preview('a');
    await vi.advanceTimersByTimeAsync(30);
    http.expectOne(() => true).flush({ previews: { a: doc } });
    await first;
    expect(await service.preview('a')).toEqual(doc);
    http.expectNone(() => true);
  });

  it('asks again for a fresh upload until its preview is written', async () => {
    const pending = service.preview('new', { fresh: true });
    await vi.advanceTimersByTimeAsync(30);
    http.expectOne(() => true).flush({ previews: { new: null } });
    await vi.advanceTimersByTimeAsync(3030);
    http.expectOne(() => true).flush({ previews: { new: doc } });
    expect(await pending).toEqual(doc);
  });

  it('answers null for every card when the request fails', async () => {
    const pending = service.preview('a');
    await vi.advanceTimersByTimeAsync(30);
    http.expectOne(() => true).flush('nope', { status: 500, statusText: 'Server Error' });
    expect(await pending).toBeNull();
  });
});
