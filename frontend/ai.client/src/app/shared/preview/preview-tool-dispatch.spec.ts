import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { StreamParserService } from '../../session/services/chat/stream-parser.service';
import { ChatRequestService } from '../../session/services/chat/chat-request.service';
import { ChatHttpService } from '../../session/services/chat/chat-http.service';
import { MessageMapService } from '../../session/services/session/message-map.service';
import { SessionService } from '../../session/services/session/session.service';
import { UserService } from '../../auth/user.service';
import { ModelService } from '../../session/services/model/model.service';
import { ToolService } from '../../services/tool/tool.service';
import { SkillService } from '../../services/skill/skill.service';
import { FileUploadService } from '../../services/file-upload';
import { ToolApprovalService } from '../../services/tool-approval/tool-approval.service';

/**
 * Regression coverage for the preview pane's dropped-tool-call defect.
 *
 * WHAT BROKE, MEASURED (dev, 2026-09-11, agent `ast-b13ab04a0a49` against the
 * `canvas_faculty` MCP server): one requested `upload_course_file` produced ~316
 * attempts and ZERO invocations of the backing Lambda. `import_course_package`:
 * 647 attempts, zero invocations. `create_page`: 12 attempts for one requested
 * call, of which 2 reached the backend — the only reason that did not create 12
 * duplicate pages in a real course was that most attempts never landed. The
 * identical calls in the full chat succeeded on the first try. No error was
 * shown anywhere; the pane simply said "Thinking".
 *
 * WHY: the preview ran on a parallel SSE consumer that implemented 9 of the ~27
 * events `processStreamEvent` dispatches. Every callback in that parser is
 * optional and invoked with `?.`, so an event with no handler is dropped in
 * silence — no error, not even `onParseError`. Among the dropped events were
 * `tool_approval_required` and `oauth_required`: the two that *gate dispatch*.
 * An approval-gated tool call paused server-side waiting for an answer the
 * preview had no way to ask for, so the tool was never invoked at all.
 *
 * WHAT THIS PINS: a preview session is a full participant in the interrupt
 * protocol, and one requested tool call results in exactly one dispatch — not
 * zero, and not a retry storm.
 */
describe('preview tool dispatch', () => {
  const SESSION = 'preview-dispatch-spec';

  let parser: StreamParserService;
  let approvals: ToolApprovalService;
  let sendChatRequest: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    TestBed.resetTestingModule();
    sendChatRequest = vi.fn().mockResolvedValue(undefined);

    TestBed.configureTestingModule({
      providers: [
        ChatRequestService,
        { provide: ChatHttpService, useValue: { sendChatRequest } },
        { provide: Router, useValue: { navigate: vi.fn() } },
        {
          provide: MessageMapService,
          useValue: {
            addUserMessage: vi.fn(),
            startStreaming: vi.fn(),
            beginContinuationStreaming: vi.fn(),
            endStreaming: vi.fn(),
            reloadMessagesForSession: vi.fn().mockResolvedValue(undefined),
          },
        },
        { provide: SessionService, useValue: { addSessionToCache: vi.fn(), applyServerTitle: vi.fn(), isNewSession: () => false } },
        { provide: UserService, useValue: { getUser: () => ({ user_id: 'u1' }) } },
        {
          provide: ModelService,
          useValue: {
            getSelectedModel: () => ({ modelId: 'm', provider: 'bedrock' }),
            isUsingDefaultModel: () => true,
            getInferenceParamOverrides: () => ({}),
          },
        },
        { provide: ToolService, useValue: { getEnabledToolIds: () => [] } },
        { provide: SkillService, useValue: { getEnabledSkillIds: () => [] } },
        { provide: FileUploadService, useValue: { getReadyFileById: vi.fn() } },
      ],
    });

    // Instantiating ChatRequestService is what installs the resume handlers on
    // ToolApprovalService / OAuthConsentService — the wiring under test.
    TestBed.inject(ChatRequestService);
    parser = TestBed.inject(StreamParserService);
    approvals = TestBed.inject(ToolApprovalService);
    parser.reset(SESSION);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  /** Drive the SSE a paused, approval-gated tool call actually produces. */
  function streamOneGatedToolCall(): void {
    parser.parseEventSourceMessage(SESSION, 'message_start', { role: 'assistant' });
    parser.parseEventSourceMessage(SESSION, 'tool_use', {
      tool_use: {
        tool_use_id: 'tu-1',
        name: 'upload_course_file',
        input: JSON.stringify({ course_id: '123', content: 'BASE64' }),
      },
    });
    parser.parseEventSourceMessage(SESSION, 'tool_approval_required', {
      type: 'tool_approval_required',
      interruptId: 'int-1',
      toolUseId: 'tu-1',
      toolName: 'upload_course_file',
      toolInput: { course_id: '123' },
      message: 'Approve upload_course_file?',
    });
    parser.parseEventSourceMessage(SESSION, 'done', {});
  }

  /**
   * THE test. Before the fix this asserted zero: the event reached a callback
   * slot that did not exist, and nothing anywhere recorded that a tool was
   * waiting on the user.
   */
  it('surfaces an approval-gated tool call instead of dropping it', () => {
    streamOneGatedToolCall();

    const pending = approvals.pending();
    expect(pending).toHaveLength(1);
    expect(pending[0].toolName).toBe('upload_course_file');
    expect(pending[0].sessionId).toBe(SESSION);
  });

  it('dispatches exactly one resume for one approved tool call', async () => {
    streamOneGatedToolCall();
    sendChatRequest.mockClear();

    await approvals.resolve('int-1', 'approved');

    expect(sendChatRequest).toHaveBeenCalledTimes(1);
    expect(sendChatRequest).toHaveBeenCalledWith(
      expect.objectContaining({
        session_id: SESSION,
        interrupt_responses: [{ interruptId: 'int-1', response: 'approved' }],
      }),
    );
    expect(approvals.pending()).toHaveLength(0);
  });

  /**
   * A retried or duplicated `tool_approval_required` for the same interrupt must
   * not stack up a second prompt — two prompts would resume the same turn twice,
   * which is the `create_page`-fires-12-times failure mode with the dispatch
   * actually working.
   */
  it('does not queue a second approval when the same interrupt is re-announced', () => {
    streamOneGatedToolCall();
    parser.parseEventSourceMessage(SESSION, 'tool_approval_required', {
      type: 'tool_approval_required',
      interruptId: 'int-1',
      toolUseId: 'tu-1',
      toolName: 'upload_course_file',
      toolInput: { course_id: '123' },
      message: 'Approve upload_course_file?',
    });

    expect(approvals.pending()).toHaveLength(1);
  });

  it('dispatches nothing when the user declines', async () => {
    streamOneGatedToolCall();
    sendChatRequest.mockClear();

    await approvals.resolve('int-1', 'declined');

    // Still exactly one request — the decline is carried to the paused turn so
    // the model is told the tool was refused, rather than left hanging.
    expect(sendChatRequest).toHaveBeenCalledTimes(1);
    expect(sendChatRequest).toHaveBeenCalledWith(
      expect.objectContaining({
        interrupt_responses: [{ interruptId: 'int-1', response: 'declined' }],
      }),
    );
  });

  /**
   * A preview session has no persisted metadata row — the backend skips those
   * writes for `preview-` ids — so reconciling from the server would replace a
   * correct in-memory transcript with an empty one. Resume is in-memory here.
   */
  it('resumes in memory without re-reading a session that was never persisted', async () => {
    const messageMap = TestBed.inject(MessageMapService) as unknown as {
      reloadMessagesForSession: ReturnType<typeof vi.fn>;
    };
    streamOneGatedToolCall();

    await approvals.resolve('int-1', 'approved');

    expect(messageMap.reloadMessagesForSession).not.toHaveBeenCalled();
  });
});
