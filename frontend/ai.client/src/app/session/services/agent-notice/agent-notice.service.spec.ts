import { describe, it, expect, beforeEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { AgentNoticeService } from './agent-notice.service';
import { AgentNoticeEvent } from '../../../shared/utils/stream-parser';

function notice(message: string, sessionId = 's1'): AgentNoticeEvent {
  return { type: 'agent_notice', sessionId, agentId: 'ast-1', message, unavailableTools: ['canvas'], unavailableSkills: [] };
}

describe('AgentNoticeService', () => {
  let service: AgentNoticeService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    service = TestBed.inject(AgentNoticeService);
  });

  it('keeps each session’s notice to itself', () => {
    service.set('s1', notice('Without Canvas.'));
    expect(service.noticeFor('s1')?.message).toBe('Without Canvas.');
    expect(service.noticeFor('s2')).toBeNull();
    expect(service.noticeFor(null)).toBeNull();
  });

  it('stays dismissed while later turns repeat the same message', () => {
    service.set('s1', notice('Without Canvas.'));
    service.dismiss('s1');
    expect(service.noticeFor('s1')).toBeNull();

    service.set('s1', notice('Without Canvas.'));
    expect(service.noticeFor('s1')).toBeNull();
  });

  it('shows again when the message changes', () => {
    service.set('s1', notice('Without Canvas.'));
    service.dismiss('s1');
    service.set('s1', notice('Without Canvas or your usual model.'));
    expect(service.noticeFor('s1')?.message).toBe('Without Canvas or your usual model.');
  });
});
