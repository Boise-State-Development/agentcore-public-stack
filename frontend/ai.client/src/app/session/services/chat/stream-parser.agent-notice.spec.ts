// A project's agent can run a member's turn without some of its setup (a tool,
// skill, model or memory that member can't use). The backend says so with an
// `agent_notice` before `message_start`; it is not persisted, so the live
// stream is the only place the SPA ever sees it.
import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { StreamParserService } from './stream-parser.service';
import { ChatStateService } from './chat-state.service';
import { ErrorService } from '../../../services/error/error.service';
import { QuotaWarningService } from '../../../services/quota/quota-warning.service';
import { AgentNoticeService } from '../agent-notice/agent-notice.service';

const NOTICE = {
  type: 'agent_notice',
  sessionId: 's1',
  agentId: 'ast-1',
  projectId: 'prj_1',
  message: 'This task runs without Canvas, which you don’t have access to.',
  unavailableTools: ['canvas'],
  unavailableSkills: [],
};

describe('StreamParserService - agent_notice', () => {
  let service: StreamParserService;
  let notices: AgentNoticeService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [StreamParserService, ChatStateService, ErrorService, QuotaWarningService],
    });
    service = TestBed.inject(StreamParserService);
    notices = TestBed.inject(AgentNoticeService);
  });

  afterEach(() => TestBed.resetTestingModule());

  it('records the notice for the stream’s own session, before the reply starts', () => {
    service.reset('s1');
    service.parseEventSourceMessage('s1', 'agent_notice', NOTICE);
    expect(notices.noticeFor('s1')?.message).toBe(NOTICE.message);
    expect(notices.noticeFor('s2')).toBeNull();
  });

  it('drops a malformed notice', () => {
    service.reset('s1');
    service.parseEventSourceMessage('s1', 'agent_notice', { ...NOTICE, message: '' });
    expect(notices.noticeFor('s1')).toBeNull();
  });
});
