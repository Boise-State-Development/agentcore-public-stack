import { describe, it, expect, beforeEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { AgentNoticeBannerComponent } from './agent-notice-banner.component';
import { AgentNoticeService } from '../../services/agent-notice/agent-notice.service';
import { SessionService } from '../../services/session/session.service';

describe('AgentNoticeBannerComponent', () => {
  const currentSession = signal({ sessionId: 's1' });
  let notices: AgentNoticeService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    currentSession.set({ sessionId: 's1' });
    TestBed.configureTestingModule({
      imports: [AgentNoticeBannerComponent],
      providers: [{ provide: SessionService, useValue: { currentSession } }],
    });
    notices = TestBed.inject(AgentNoticeService);
  });

  function render(): HTMLElement {
    const fixture = TestBed.createComponent(AgentNoticeBannerComponent);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('renders nothing without a notice', () => {
    expect(render().textContent?.trim()).toBe('');
  });

  it('shows the server’s message for the viewed conversation only, and dismisses it', () => {
    notices.set('s1', {
      type: 'agent_notice', sessionId: 's1', agentId: 'ast-1',
      message: 'This task runs without Canvas, which you don’t have access to.',
      unavailableTools: ['canvas'], unavailableSkills: [],
    });
    notices.set('s2', {
      type: 'agent_notice', sessionId: 's2', agentId: 'ast-1', message: 'Other task.',
      unavailableTools: [], unavailableSkills: [],
    });
    const fixture = TestBed.createComponent(AgentNoticeBannerComponent);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('[role=status]')?.textContent).toContain('runs without Canvas');
    expect(el.textContent).not.toContain('Other task.');

    el.querySelector<HTMLButtonElement>('button[aria-label="Dismiss notice"]')!.click();
    fixture.detectChanges();
    expect(el.querySelector('[role=status]')).toBeNull();
    expect(notices.noticeFor('s2')?.message).toBe('Other task.');
  });
});
