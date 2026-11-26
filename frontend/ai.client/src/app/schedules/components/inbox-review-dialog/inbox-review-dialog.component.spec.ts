import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { expectNamedDialog, openInCdkDialog } from '../../../../testing/cdk-dialog';
import { ConfigService } from '../../../services/config.service';
import { FEATURES } from '../../../services/features';
import { AppNotification, InboxPrompt } from '../../../services/notifications/notification.model';
import { environment } from '../../../../environments/environment';
import { FeatureFlags } from '../../../../environments/feature-flags';
import {
  InboxReviewDialogComponent,
  InboxReviewDialogData,
  InboxReviewDialogResult,
} from './inbox-review-dialog.component';
import { inboxPromptView } from './inbox-prompt-slot.component';

const BASE = 'http://localhost:8000/notifications/n1';

const ROW: AppNotification = {
  notificationId: 'n1',
  recipientEmail: 'me@x.edu',
  kind: 'agent_needs_input',
  payload: {
    scheduleId: 'sch-1',
    runId: 'run-1',
    sessionId: 'headless-7',
    label: 'Inbox watcher',
    runFinishedAt: new Date(Date.now() - 2 * 3_600_000).toISOString(),
    expiresAt: new Date(Date.now() + 6 * 86_400_000 + 3_600_000).toISOString(),
    answerable: true,
    prompts: [],
  },
  createdAt: new Date().toISOString(),
  readAt: null,
};

const APPROVAL: InboxPrompt = {
  interruptId: 'i-approve',
  kind: 'tool_approval',
  toolUseId: 'tu-1',
  toolName: 'send_message',
  toolInput: '{"to":"team@example.edu","body":"Draft reply"}',
};

const QUESTION: InboxPrompt = {
  interruptId: 'i-ask',
  kind: 'user_question',
  toolUseId: 'tu-2',
  questions: JSON.stringify([
    { header: 'Calendar', question: 'Which calendar?', multiSelect: false, options: [{ label: 'Work' }, { label: 'Home' }] },
  ]),
};

const FORM: InboxPrompt = {
  interruptId: 'i-form',
  kind: 'mcp_elicitation',
  toolUseId: 'tu-3',
  elicitation: JSON.stringify({
    type: 'mcp_elicitation_required',
    interruptId: 'i-form',
    toolUseId: 'tu-3',
    toolName: 'create_repository',
    serverName: 'GitHub',
    message: 'Name the repository',
    fields: [{ name: 'repository_name', kind: 'text', title: 'Repository name', required: true }],
  }),
};

describe('InboxReviewDialogComponent', () => {
  let http: HttpTestingController;

  function configure(features: Partial<FeatureFlags> = {}) {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
        { provide: FEATURES, useValue: { ...environment.features, proactiveRuns: true, ...features } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
  });

  afterEach(() => {
    TestBed.inject(Dialog).closeAll();
    http.verify();
  });

  async function open(prompts: InboxPrompt[]) {
    const opened = await openInCdkDialog<InboxReviewDialogComponent, InboxReviewDialogData, InboxReviewDialogResult>(
      InboxReviewDialogComponent,
      { data: { notification: ROW } },
    );
    const result = firstValueFrom(opened.ref.closed);
    http.expectOne(`${BASE}/prompts`).flush({ notificationId: 'n1', sessionId: 'headless-7', prompts });
    await settle();
    return { ...opened, result };
  }

  async function settle() {
    for (let i = 0; i < 3; i++) {
      await new Promise((resolve) => setTimeout(resolve, 0));
      TestBed.tick();
    }
  }

  function button(container: HTMLElement, text: string | RegExp): HTMLButtonElement {
    const found = Array.from(container.querySelectorAll('button')).find((b) =>
      typeof text === 'string' ? b.textContent?.trim() === text || b.getAttribute('aria-label') === text : text.test(b.textContent ?? ''),
    );
    if (!found) throw new Error(`no button ${text}`);
    return found as HTMLButtonElement;
  }

  it('is a named dialog that names the schedule, when it paused and when it expires', async () => {
    configure();
    const { container } = await open([APPROVAL]);
    expectNamedDialog(container, {
      name: '“Inbox watcher” is waiting for you',
      description: /paused 2h ago to ask you\. Answer here and it carries on in the background\. Expires in 6 days\./,
    });
    expect(button(container, 'Open conversation')).toBeTruthy();
  });

  it('one approval sends on Approve, with the card’s own answer, and closes with the run to track', async () => {
    configure();
    const { container, result } = await open([APPROVAL]);
    expect(container.textContent).toContain('send_message');
    button(container, 'Approve tool call').click();
    await settle();
    const req = http.expectOne(`${BASE}/respond`);
    expect(req.request.body).toEqual({ responses: [{ interruptId: 'i-approve', response: 'approved' }] });
    const resolution = { state: 'applying', decision: 'approved', at: '2026-11-26T12:00:00Z', resumeRunId: 'r-1' };
    req.flush({ resumeRunId: 'r-1', resolution }, { status: 202, statusText: 'Accepted' });
    expect(await result).toEqual({ kind: 'sent', resumeRunId: 'r-1', resolution });
  });

  it('several prompts wait for Send, and go in one request in the row’s order', async () => {
    configure();
    const { container, result } = await open([APPROVAL, QUESTION]);
    const send = button(container, 'Send answers');
    expect(send.disabled).toBe(true);

    button(container, 'Decline tool call').click();
    await settle();
    expect(container.querySelector('[data-testid="inbox-answered"]')?.textContent).toContain('Declined');
    expect(send.disabled).toBe(true);

    button(container, 'Skip').click();
    await settle();
    expect(send.disabled).toBe(false);
    http.expectNone(`${BASE}/respond`);

    send.click();
    await settle();
    const req = http.expectOne(`${BASE}/respond`);
    expect(req.request.body).toEqual({
      responses: [
        { interruptId: 'i-approve', response: 'declined' },
        { interruptId: 'i-ask', response: { skipped: true } },
      ],
    });
    req.flush({ resumeRunId: 'r-2', resolution: { state: 'applying', at: 'x', resumeRunId: 'r-2' } }, { status: 202, statusText: 'Accepted' });
    expect((await result)?.kind).toBe('sent');
  });

  it('Change takes an answer back and brings its card back, empty', async () => {
    configure();
    const { container } = await open([APPROVAL, QUESTION]);
    button(container, 'Approve tool call').click();
    await settle();
    button(container, /Change/).click();
    await settle();
    expect(container.querySelector('[data-testid="inbox-answered"]')).toBeNull();
    expect(button(container, 'Approve tool call').disabled).toBe(false);
    expect(button(container, 'Send answers').disabled).toBe(true);
  });

  it('a 410 on load closes it as expired; a 409 on send closes it as already answered', async () => {
    configure();
    const first = await openInCdkDialog<InboxReviewDialogComponent, InboxReviewDialogData, InboxReviewDialogResult>(
      InboxReviewDialogComponent,
      { data: { notification: ROW } },
    );
    const closed = firstValueFrom(first.ref.closed);
    http
      .expectOne(`${BASE}/prompts`)
      .flush({ detail: { code: 'expired', message: 'This expired before it was answered.' } }, { status: 410, statusText: 'Gone' });
    expect(await closed).toEqual({ kind: 'settled', state: 'expired', message: 'This expired before it was answered.' });

    const { container, result } = await open([APPROVAL]);
    button(container, 'Approve tool call').click();
    await settle();
    http
      .expectOne(`${BASE}/respond`)
      .flush({ detail: { code: 'already_answered', message: 'This was already answered.' } }, { status: 409, statusText: 'Conflict' });
    expect(await result).toEqual({ kind: 'settled', state: 'answered_elsewhere', message: 'This was already answered.' });
  });

  it('a send the server couldn’t take says so and gives the card back', async () => {
    configure();
    const { container } = await open([APPROVAL]);
    button(container, 'Approve tool call').click();
    await settle();
    http
      .expectOne(`${BASE}/respond`)
      .flush({ detail: { code: 'unavailable', message: 'Couldn’t send the answer. Try again.' } }, { status: 503, statusText: 'Unavailable' });
    await settle();
    expect(container.querySelector('[data-testid="inbox-send-error"]')?.textContent).toContain('Couldn’t send the answer');
    expect(button(container, 'Approve tool call').disabled).toBe(false);
  });

  it('renders a form only where forms are on; otherwise it can be declined here', async () => {
    configure({ mcpElicitation: false });
    const { container, result } = await open([FORM]);
    expect(container.querySelector('[data-testid="inbox-form-off"]')?.textContent).toContain('GitHub asks you to fill in a form');
    button(container, 'Decline').click();
    await settle();
    const req = http.expectOne(`${BASE}/respond`);
    expect(req.request.body).toEqual({ responses: [{ interruptId: 'i-form', response: { action: 'decline' } }] });
    req.flush({ resumeRunId: 'r-3', resolution: { state: 'applying', at: 'x' } }, { status: 202, statusText: 'Accepted' });
    expect((await result)?.kind).toBe('sent');
  });

  it('renders the server’s own form where forms are on', async () => {
    configure({ mcpElicitation: true });
    const { container } = await open([FORM]);
    expect(container.textContent).toContain('Repository name');
    expect(container.querySelector('[data-testid="inbox-form-off"]')).toBeNull();
  });

  it('Open conversation closes it so the caller can navigate', async () => {
    configure();
    const { container, result } = await open([APPROVAL]);
    button(container, 'Open conversation').click();
    expect(await result).toEqual({ kind: 'open' });
  });
});

describe('inboxPromptView', () => {
  it('turns each breadcrumb into the request its card takes', () => {
    const approval = inboxPromptView(APPROVAL, 'headless-7', true);
    expect(approval.kind).toBe('approval');
    if (approval.kind === 'approval') {
      expect(approval.request).toMatchObject({ interruptId: 'i-approve', toolName: 'send_message', sessionId: 'headless-7' });
      expect(approval.request.toolInput).toContain('Draft reply');
    }
    expect(inboxPromptView(QUESTION, 's', true).kind).toBe('question');
    expect(inboxPromptView(FORM, 's', true).kind).toBe('form');
    expect(inboxPromptView(FORM, 's', false)).toEqual({ kind: 'form_off', interruptId: 'i-form', serverName: 'GitHub' });
  });

  it('a prompt that would not render in the conversation is answered there instead', () => {
    expect(inboxPromptView({ ...QUESTION, questions: '{not json' }, 's', true).kind).toBe('elsewhere');
    expect(inboxPromptView({ ...QUESTION, questions: '[]' }, 's', true).kind).toBe('elsewhere');
    expect(inboxPromptView({ ...FORM, elicitation: '{}' }, 's', true).kind).toBe('elsewhere');
    expect(inboxPromptView({ interruptId: 'b', kind: 'browser_login' }, 's', true).kind).toBe('elsewhere');
    expect(inboxPromptView({ interruptId: 'o', kind: 'oauth' }, 's', true).kind).toBe('elsewhere');
  });
});
