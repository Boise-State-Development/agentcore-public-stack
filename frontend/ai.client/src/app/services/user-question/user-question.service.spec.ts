import { TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  UserQuestionResponse,
  UserQuestionService,
} from './user-question.service';
import type { UserQuestion } from '../../shared/utils/stream-parser/stream-parser-types';

/**
 * The service owns one fact: a prompt is surfaced once, resolved once, and the
 * resolution reaches the chat layer. The subtlety is that its queue is global
 * while prompts belong to a session — the message list filters on that, and
 * this suite pins the `sessionId` the filter depends on being carried.
 */
const QUESTIONS: UserQuestion[] = [
  {
    header: 'Scope',
    question: 'How much should this cover?',
    multiSelect: false,
    options: [{ label: 'Just the API' }, { label: 'Everything' }],
  },
];

function request(overrides: Partial<Parameters<UserQuestionService['requestAnswers']>[0]> = {}) {
  return {
    interruptId: 'v1:tool_call:tu-1:abc',
    toolUseId: 'tu-1',
    questions: QUESTIONS,
    sessionId: 's1',
    ...overrides,
  };
}

describe('UserQuestionService', () => {
  let service: UserQuestionService;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [UserQuestionService] });
    service = TestBed.inject(UserQuestionService);
  });

  describe('surfacing', () => {
    it('exposes a registered prompt as pending', () => {
      service.requestAnswers(request());

      expect(service.hasPending()).toBe(true);
      const [pending] = service.pending();
      expect(pending.interruptId).toBe('v1:tool_call:tu-1:abc');
      expect(pending.questions).toEqual(QUESTIONS);
      // The message list filters the global queue on this; without it a
      // prompt would render in whichever pane happened to be mounted.
      expect(pending.sessionId).toBe('s1');
    });

    it('is idempotent for the same interrupt id', () => {
      service.requestAnswers(request());
      service.requestAnswers(request());

      expect(service.pending()).toHaveLength(1);
    });

    it('does not resurrect a resolved prompt on stream replay', async () => {
      service.setResumeHandler(vi.fn());
      service.requestAnswers(request());
      await service.resolve('v1:tool_call:tu-1:abc', { answers: {} });

      service.requestAnswers(request());

      expect(service.hasPending()).toBe(false);
    });

    it('surfaces a genuine second round (fresh interrupt id)', () => {
      service.requestAnswers(request());
      service.requestAnswers(
        request({ interruptId: 'v1:tool_call:tu-2:def', toolUseId: 'tu-2' }),
      );

      expect(service.pending()).toHaveLength(2);
    });

    it('ignores a prompt with no questions', () => {
      service.requestAnswers(request({ questions: [] }));

      expect(service.hasPending()).toBe(false);
    });

    it('orders prompts by arrival', () => {
      service.requestAnswers(request({ interruptId: 'a' }));
      service.requestAnswers(request({ interruptId: 'b' }));

      expect(service.pending().map((r) => r.interruptId)).toEqual(['a', 'b']);
    });
  });

  describe('resolution', () => {
    it('forwards answers to the resume handler with the session', async () => {
      const handler = vi.fn();
      service.setResumeHandler(handler);
      service.requestAnswers(request());

      const response: UserQuestionResponse = {
        answers: { Scope: { selected: ['Everything'] } },
      };
      await service.resolve('v1:tool_call:tu-1:abc', response);

      expect(handler).toHaveBeenCalledWith('v1:tool_call:tu-1:abc', response, {
        sessionId: 's1',
      });
      expect(service.hasPending()).toBe(false);
    });

    it('skip posts an object, never null', async () => {
      const handler = vi.fn();
      service.setResumeHandler(handler);
      service.requestAnswers(request());

      await service.skip('v1:tool_call:tu-1:abc');

      // The backend's ToolContext.interrupt only treats a non-null response
      // as an answer — a null would re-raise the same interrupt forever and
      // strand the user in a paused turn.
      const [, response] = handler.mock.calls[0];
      expect(response).toEqual({ skipped: true });
      expect(response).not.toBeNull();
    });

    it('a second resolve is a no-op', async () => {
      const handler = vi.fn();
      service.setResumeHandler(handler);
      service.requestAnswers(request());

      await service.resolve('v1:tool_call:tu-1:abc', { skipped: true });
      await service.resolve('v1:tool_call:tu-1:abc', { skipped: true });

      expect(handler).toHaveBeenCalledTimes(1);
    });

    it('drops the prompt even when no handler is registered', async () => {
      service.requestAnswers(request());

      await service.resolve('v1:tool_call:tu-1:abc', { skipped: true });

      expect(service.hasPending()).toBe(false);
    });

    it('a throwing handler does not resurrect the prompt', async () => {
      service.setResumeHandler(vi.fn().mockRejectedValue(new Error('network')));
      service.requestAnswers(request());

      await service.resolve('v1:tool_call:tu-1:abc', { skipped: true });

      // The turn may be stuck server-side, but re-rendering a prompt whose
      // answers were already sent would double-post on the retry.
      expect(service.hasPending()).toBe(false);
    });

    it('resolving an unknown id does not call the handler', async () => {
      const handler = vi.fn();
      service.setResumeHandler(handler);

      await service.resolve('nope', { skipped: true });

      expect(handler).not.toHaveBeenCalled();
    });
  });
});
