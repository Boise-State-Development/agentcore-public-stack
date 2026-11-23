import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { SUPPRESS_ERROR_TOAST } from '../../auth/error.interceptor';
import { ConfigService } from '../config.service';
import { WorkflowWrite, WorkflowsApiService } from './workflows-api.service';

const REF = { skillId: 'SKL-1', name: 'proposal-review' };
const BODY: WorkflowWrite = { title: 'Review', files: 'any', steps: [], result: { mode: 'card' }, expectedVersion: 3 };

describe('WorkflowsApiService, the designer’s calls (PR-D.3)', () => {
  let http: HttpTestingController;
  let service: WorkflowsApiService;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    TestBed.inject(ConfigService).appApiUrl.set('/api');
    http = TestBed.inject(HttpTestingController);
    service = TestBed.inject(WorkflowsApiService);
  });

  afterEach(() => http.verify());

  it('maps a stale save’s 409 to a conflict carrying what is stored now, with no global toast', async () => {
    const saving = service.write(REF, BODY);
    const req = http.expectOne('/api/workflows/SKL-1/proposal-review');
    expect(req.request.method).toBe('PUT');
    expect(req.request.body.expectedVersion).toBe(3);
    expect(req.request.context.get(SUPPRESS_ERROR_TOAST)).toBe(true);
    const stored = { workflow: { version: 4 }, sheet: { startable: true, blockers: [] } };
    req.flush({ detail: 'changed', code: 'version_conflict', document: stored }, { status: 409, statusText: 'Conflict' });

    expect(await saving).toEqual({
      ok: false,
      conflict: true,
      message: 'This workflow changed somewhere else since you opened it.',
      document: stored,
    });
  });

  it('maps a save against a deleted workflow to a conflict with no document', async () => {
    const saving = service.write(REF, BODY);
    http
      .expectOne('/api/workflows/SKL-1/proposal-review')
      .flush({ detail: 'gone', code: 'workflow_deleted', document: null }, { status: 409, statusText: 'Conflict' });
    expect(await saving).toMatchObject({ ok: false, conflict: true, document: null, message: 'This workflow was deleted somewhere else.' });
  });

  it('reports any other refusal in the server’s words', async () => {
    const saving = service.write(REF, BODY);
    http.expectOne('/api/workflows/SKL-1/proposal-review').flush({ detail: 'a step reads itself' }, { status: 400, statusText: 'Bad Request' });
    expect(await saving).toEqual({ ok: false, conflict: false, message: 'a step reads itself' });
  });

  it('deletes a workflow, with keepalive only when asked, so a delete sent as the tab closes outlives it', async () => {
    const plain = service.remove(REF);
    const first = http.expectOne('/api/workflows/SKL-1/proposal-review');
    expect(first.request.method).toBe('DELETE');
    expect(first.request.keepalive).toBe(false);
    first.flush(null, { status: 204, statusText: 'No Content' });
    await plain;

    const closing = service.remove(REF, { keepalive: true });
    const second = http.expectOne('/api/workflows/SKL-1/proposal-review');
    expect(second.request.keepalive).toBe(true);
    second.flush(null, { status: 204, statusText: 'No Content' });
    await closing;
  });

  it('validates the draft without its expected version, so a preview never conflicts', () => {
    void service.validate(REF, BODY);
    const req = http.expectOne('/api/workflows/SKL-1/proposal-review?validate=true');
    expect('expectedVersion' in req.request.body).toBe(false);
    req.flush({});
  });

  it('opens a file that no longer parses as a message, not an exception', async () => {
    const reading = service.read(REF);
    http
      .expectOne('/api/workflows/SKL-1/proposal-review')
      .flush({ detail: 'a step reads itself', markdown: '…' }, { status: 400, statusText: 'Bad Request' });
    expect(await reading).toEqual({ ok: false, message: 'a step reads itself' });
  });

  it('lists quietly, the caller’s own first', async () => {
    const listing = service.listQuietly();
    const req = http.expectOne('/api/workflows');
    expect(req.request.context.get(SUPPRESS_ERROR_TOAST)).toBe(true);
    req.flush({ workflows: [{ name: 'b', isOwned: false }, { name: 'a', isOwned: true }] });
    expect((await listing).map((w) => w.name)).toEqual(['a', 'b']);
  });

  it('leaves D.2’s own calls on the global toast', () => {
    void service.list();
    const req = http.expectOne('/api/workflows');
    expect(req.request.context.get(SUPPRESS_ERROR_TOAST)).toBe(false);
    req.flush({ workflows: [] });
  });
});
