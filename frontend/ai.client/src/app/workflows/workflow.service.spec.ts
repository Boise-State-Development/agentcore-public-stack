import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { SUPPRESS_ERROR_TOAST } from '../auth/error.interceptor';
import { ConfigService } from '../services/config.service';
import { WorkflowWrite } from './workflow.models';
import { WorkflowService } from './workflow.service';

const REF = { skillId: 'SKL-1', name: 'proposal-review' };
const BODY: WorkflowWrite = { title: 'Review', files: 'any', steps: [], result: { mode: 'card' }, expectedVersion: 3 };

describe('WorkflowService', () => {
  let http: HttpTestingController;
  let service: WorkflowService;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    TestBed.inject(ConfigService).appApiUrl.set('/api');
    http = TestBed.inject(HttpTestingController);
    service = TestBed.inject(WorkflowService);
  });

  afterEach(() => http.verify());

  it('maps a stale save’s 409 to a conflict carrying what is stored now, with no global toast', async () => {
    const saving = service.save(REF, BODY);
    const req = http.expectOne('/api/workflows/SKL-1/proposal-review');
    expect(req.request.method).toBe('PUT');
    expect(req.request.body.expectedVersion).toBe(3);
    expect(req.request.context.get(SUPPRESS_ERROR_TOAST)).toBe(true);
    const stored = { workflow: { version: 4 }, sheet: { startable: true, blockers: [] } };
    req.flush({ detail: 'changed', code: 'version_conflict', document: stored }, { status: 409, statusText: 'Conflict' });

    const result = await saving;
    expect(result).toEqual({ ok: false, conflict: true, message: 'This workflow changed somewhere else since you opened it.', document: stored });
  });

  it('maps a save against a deleted workflow to a conflict with no document', async () => {
    const saving = service.save(REF, BODY);
    http.expectOne('/api/workflows/SKL-1/proposal-review').flush({ detail: 'gone', code: 'workflow_deleted', document: null }, { status: 409, statusText: 'Conflict' });
    const result = await saving;
    expect(result).toMatchObject({ ok: false, conflict: true, document: null, message: 'This workflow was deleted somewhere else.' });
  });

  it('reports any other refusal in the server’s words', async () => {
    const saving = service.save(REF, BODY);
    http.expectOne('/api/workflows/SKL-1/proposal-review').flush({ detail: 'a step reads itself' }, { status: 400, statusText: 'Bad Request' });
    expect(await saving).toEqual({ ok: false, conflict: false, message: 'a step reads itself' });
  });

  it('validates the draft without its expected version, so a preview never conflicts', () => {
    void service.validate(REF, BODY);
    const req = http.expectOne('/api/workflows/SKL-1/proposal-review?validate=true');
    expect('expectedVersion' in req.request.body).toBe(false);
    req.flush({});
  });

  it('opens a file that no longer parses as a message, not an exception', async () => {
    const reading = service.read(REF);
    http.expectOne('/api/workflows/SKL-1/proposal-review').flush({ detail: 'a step reads itself', markdown: '…' }, { status: 400, statusText: 'Bad Request' });
    expect(await reading).toEqual({ ok: false, message: 'a step reads itself' });
  });
});
