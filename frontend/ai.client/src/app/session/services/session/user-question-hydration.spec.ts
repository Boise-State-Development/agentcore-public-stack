import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MessageMapService } from './message-map.service';
import { SessionService } from './session.service';
import { FileUploadService } from '../../../services/file-upload';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import { ToolApprovalService } from '../../../services/tool-approval/tool-approval.service';
import { UserQuestionService } from '../../../services/user-question/user-question.service';

/**
 * Replaying a paused clarifying-question prompt after a page refresh.
 *
 * The `user_question_required` SSE event fires once and never re-streams, so
 * without this a refresh mid-prompt orphans the turn: the picker is gone but
 * the agent is still paused waiting for an answer.
 *
 * The questions arrive as a JSON *string* (DynamoDB would coerce numbers
 * nested inside them), which makes this the one place in the SPA that parses
 * untrusted stored JSON back into a renderable prompt — so the failure modes
 * get as much attention as the happy path.
 */
const QUESTIONS = [
  {
    header: 'Scope',
    question: 'How much should this cover?',
    multiSelect: false,
    options: [{ label: 'Just the API' }, { label: 'Everything' }],
  },
];

function interrupt(overrides: Record<string, unknown> = {}) {
  return {
    interruptId: 'v1:tool_call:tu-1:abc',
    kind: 'user_question',
    toolUseId: 'tu-1',
    toolName: 'ask_user_question',
    questions: JSON.stringify(QUESTIONS),
    createdAt: '2026-09-14T00:00:00Z',
    ...overrides,
  };
}

describe('MessageMapService — user_question hydration', () => {
  let service: MessageMapService;
  let sessionService: { getMessages: ReturnType<typeof vi.fn> };
  let questionService: UserQuestionService;

  async function hydrate(pendingInterrupts: unknown[], messages: unknown[] = []) {
    sessionService.getMessages.mockResolvedValue({ messages, pendingInterrupts });
    await service.loadMessagesForSession('s1');
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    sessionService = {
      getMessages: vi.fn().mockResolvedValue({ messages: [] }),
      isNewSession: vi.fn().mockReturnValue(false),
      updateSessionTitleInCache: vi.fn(),
    } as never;
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        MessageMapService,
        UserQuestionService,
        { provide: SessionService, useValue: sessionService },
        { provide: FileUploadService, useValue: { listSessionFiles: vi.fn().mockResolvedValue([]) } },
        { provide: OAuthConsentService, useValue: { requestConsent: vi.fn() } },
        { provide: ToolApprovalService, useValue: { requestApproval: vi.fn() } },
      ],
    });
    service = TestBed.inject(MessageMapService);
    questionService = TestBed.inject(UserQuestionService);
  });

  describe('the happy path', () => {
    it('re-renders the picker from the breadcrumb', async () => {
      await hydrate([interrupt()]);

      const [pending] = questionService.pending();
      expect(pending).toBeDefined();
      expect(pending.interruptId).toBe('v1:tool_call:tu-1:abc');
      expect(pending.toolUseId).toBe('tu-1');
      expect(pending.questions).toEqual(QUESTIONS);
      // The message list filters the global queue on this.
      expect(pending.sessionId).toBe('s1');
    });

    it('anchors to the triggering message when the backend recorded one', async () => {
      await hydrate([interrupt({ triggeringMessageId: 'm-7' })]);

      expect(questionService.pending()[0].messageId).toBe('m-7');
    });

    it('falls back to the last assistant message', async () => {
      await hydrate(
        [interrupt()],
        [
          { id: 'm-1', role: 'user', content: [{ text: 'hi' }] },
          { id: 'm-2', role: 'assistant', content: [{ text: 'thinking' }] },
        ],
      );

      expect(questionService.pending()[0].messageId).toBe('m-2');
    });
  });

  describe('payloads that must not render', () => {
    it('drops an unparseable questions string', async () => {
      await hydrate([interrupt({ questions: 'not json' })]);

      expect(questionService.hasPending()).toBe(false);
    });

    it('drops a missing questions field', async () => {
      await hydrate([interrupt({ questions: undefined })]);

      expect(questionService.hasPending()).toBe(false);
    });

    it.each([
      ['empty list', '[]'],
      ['not a list', '{"header":"Scope"}'],
      ['question with no options', '[{"header":"S","question":"Which?","options":[]}]'],
      ['option with no label', '[{"header":"S","question":"Which?","options":[{}]}]'],
      ['question with no text', '[{"header":"S","question":"","options":[{"label":"a"}]}]'],
    ])('drops a payload that is %s', async (_label, questions) => {
      await hydrate([interrupt({ questions })]);

      // A picker with no answerable option is a dead end: the user can
      // neither answer nor dismiss it, and the turn stays paused. Rendering
      // nothing at least leaves them able to retype their request.
      expect(questionService.hasPending()).toBe(false);
    });
  });

  describe('coexistence with the other interrupt flavors', () => {
    it('ignores oauth and tool_approval rows', async () => {
      await hydrate([
        { interruptId: 'o1', kind: 'oauth', providerId: 'google', createdAt: 'x' },
        { interruptId: 't1', kind: 'tool_approval', toolName: 'send_email', createdAt: 'x' },
      ]);

      expect(questionService.hasPending()).toBe(false);
    });

    it('hydrates a user_question alongside them', async () => {
      await hydrate([
        { interruptId: 'o1', kind: 'oauth', providerId: 'google', createdAt: 'x' },
        interrupt(),
      ]);

      expect(questionService.pending()).toHaveLength(1);
      expect(questionService.pending()[0].interruptId).toBe('v1:tool_call:tu-1:abc');
    });

    it('does not re-add a prompt the user already answered this session', async () => {
      await hydrate([interrupt()]);
      await questionService.resolve('v1:tool_call:tu-1:abc', { skipped: true });

      await hydrate([interrupt()]);

      expect(questionService.hasPending()).toBe(false);
    });
  });
});
