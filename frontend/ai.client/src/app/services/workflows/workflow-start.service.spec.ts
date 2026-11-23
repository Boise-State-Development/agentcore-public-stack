import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { UserService } from '../../auth/user.service';
import { WorkflowRunService } from '../../session/services/handoff/workflow-run.service';
import { MessageMapService } from '../../session/services/session/message-map.service';
import { SessionPrewarmService } from '../../session/services/session/session-prewarm.service';
import { SessionService } from '../../session/services/session/session.service';
import { ConfigService } from '../config.service';
import { ToastService } from '../toast/toast.service';
import { WorkflowStartService } from './workflow-start.service';
import { WorkflowDocument, WorkflowSummary } from './workflow.models';
import { definitionOf, sortOwnedFirst } from './workflows-api.service';

const API = 'https://api.test';
const REF = { skillId: 'skill-1', name: 'review' };

const DOC: WorkflowDocument = {
  workflow: {
    ...REF,
    title: 'Proposal review',
    version: 3,
    files: 'one',
    request: 'Review this proposal',
    steps: [
      {
        name: 'generalist',
        kind: 'agent',
        agentId: 'a1',
        agentVersion: 5,
        reads: ['request', 'files'],
        effectiveRequest: 'Review this proposal',
        agent: { canRun: true, name: 'Generalist', emoji: '🧭', hasIcon: false, changed: false, runsDraft: false },
      },
      {
        name: 'colleague',
        kind: 'agent',
        agentId: 'a2',
        reads: ['request', 'files'],
        request: 'Read it as a colleague',
        effectiveRequest: 'Read it as a colleague',
        agent: { canRun: true, name: 'Colleague', hasIcon: false, changed: false, runsDraft: false },
      },
    ],
    result: { mode: 'card', steps: ['generalist', 'colleague'] },
    fingerprint: 'fp',
    isOwned: true,
  },
  sheet: { startable: true, blockers: [] },
};

describe('WorkflowStartService', () => {
  let service: WorkflowStartService;
  let http: HttpTestingController;
  let calls: string[];
  let messages: unknown[];
  let newSessions: Set<string>;
  const open = vi.fn();
  const navigate = vi.fn();
  const updateSessionMetadata = vi.fn();
  const addSessionToCache = vi.fn();
  const applyServerTitle = vi.fn();
  const toastError = vi.fn();

  beforeEach(() => {
    calls = [];
    messages = [];
    newSessions = new Set();
    for (const fn of [open, navigate, updateSessionMetadata, addSessionToCache, applyServerTitle, toastError]) fn.mockReset();
    open.mockImplementation(async (id: string) => void calls.push(`open:${id}`));
    navigate.mockImplementation(async (commands: string[]) => {
      calls.push(`navigate:${commands.join('/')}`);
      return true;
    });
    updateSessionMetadata.mockImplementation(async (id: string, body: { title: string }) => {
      calls.push(`put:${id}:${body.title}`);
      return {};
    });
    addSessionToCache.mockImplementation((id: string, _user: string, title: string) => calls.push(`cache:${id}:${title}`));
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        { provide: WorkflowRunService, useValue: { open } },
        {
          provide: SessionService,
          useValue: {
            updateSessionMetadata,
            addSessionToCache,
            applyServerTitle,
            isNewSession: (id: string) => newSessions.has(id),
          },
        },
        { provide: MessageMapService, useValue: { getMessagesForSession: () => signal(messages) } },
        { provide: SessionPrewarmService, useValue: { claimNewConversationId: () => 'new-conv' } },
        { provide: UserService, useValue: { currentUser: () => ({ user_id: 'u1' }) } },
        { provide: Router, useValue: { navigate } },
        { provide: ToastService, useValue: { error: toastError } },
      ],
    });
    service = TestBed.inject(WorkflowStartService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function readDoc(result: Promise<string | null>, body: WorkflowDocument = DOC, status = 200): Promise<string | null> {
    // The read is the first thing a start does, before any conversation is touched.
    expect(calls).toEqual([]);
    const req = http.expectOne(`${API}/workflows/skill-1/review`);
    expect(req.request.method).toBe('GET');
    if (status === 200) req.flush(body);
    else req.flush({ detail: 'Workflow not found' }, { status, statusText: 'x' });
    return result;
  }

  it('in a new conversation: creates its row titled with the workflow, then opens the sheet there', async () => {
    const sessionId = await readDoc(service.start(REF));

    expect(sessionId).toBe('new-conv');
    expect(calls).toEqual(['put:new-conv:Proposal review', 'cache:new-conv:Proposal review', 'navigate:s/new-conv', 'open:new-conv']);
    const [, draft] = open.mock.calls[0];
    expect(draft.workflow).toEqual({ ref: REF, document: DOC });
    expect(draft.request).toBe('Review this proposal');
    expect(draft.agents).toEqual([
      { agentId: 'a1', agentName: 'Generalist', emoji: '🧭', iconUrl: undefined },
      { agentId: 'a2', agentName: 'Colleague', emoji: undefined, iconUrl: undefined },
    ]);
  });

  it('a test run: a new conversation titled "Test: …", never navigated to and never the prewarmed id', async () => {
    const sessionId = await readDoc(service.start(REF, { navigate: false, titlePrefix: 'Test: ' }));

    expect(sessionId).not.toBe('new-conv');
    expect(sessionId).toMatch(/^[0-9a-f-]{36}$/);
    expect(calls).toEqual([`put:${sessionId}:Test: Proposal review`, `cache:${sessionId}:Test: Proposal review`, `open:${sessionId}`]);
    expect(navigate).not.toHaveBeenCalled();
  });

  it('in a conversation with turns: opens the sheet there and leaves its title alone', async () => {
    messages = [{ role: 'user' }];
    newSessions.add('src');
    const sessionId = await readDoc(service.start(REF, { sessionId: 'src' }));

    expect(sessionId).toBe('src');
    expect(calls).toEqual(['open:src']);
  });

  it('in a conversation this tab staged that has no turns: gives it its row first', async () => {
    newSessions.add('staged');
    await readDoc(service.start(REF, { sessionId: 'staged' }));

    expect(calls).toEqual(['put:staged:Proposal review', 'open:staged']);
    expect(applyServerTitle).toHaveBeenCalledWith('staged', 'Proposal review');
  });

  it('a workflow that can’t be read leaves no conversation behind', async () => {
    const sessionId = await readDoc(service.start(REF), DOC, 404);

    expect(sessionId).toBeNull();
    expect(calls).toEqual([]);
    expect(toastError).toHaveBeenCalledWith('This workflow couldn’t be opened', expect.stringContaining('deleted'));
  });

  it('a row that can’t be created opens no sheet', async () => {
    updateSessionMetadata.mockRejectedValueOnce(new Error('500'));
    const sessionId = await readDoc(service.start(REF));

    expect(sessionId).toBeNull();
    expect(open).not.toHaveBeenCalled();
  });
});

describe('the workflows API helpers', () => {
  it('lists the caller’s own workflows first', () => {
    const row = (name: string, isOwned: boolean): WorkflowSummary => ({ skillId: 's', name, skillName: 'S', isOwned, designerMade: true });
    expect(sortOwnedFirst([row('a', false), row('b', true), row('c', false), row('d', true)]).map((w) => w.name)).toEqual(['b', 'd', 'a', 'c']);
  });

  it('turns a document back into the PUT body, without the server’s version', () => {
    const body = definitionOf(DOC.workflow);
    expect(body).toEqual({
      title: 'Proposal review',
      files: 'one',
      request: 'Review this proposal',
      steps: [
        { name: 'generalist', kind: 'agent', agentId: 'a1', agentVersion: 5, reads: ['request', 'files'], request: null },
        { name: 'colleague', kind: 'agent', agentId: 'a2', agentVersion: null, reads: ['request', 'files'], request: 'Read it as a colleague' },
      ],
      result: { mode: 'card', reads: null, instruction: null },
    });
  });
});
