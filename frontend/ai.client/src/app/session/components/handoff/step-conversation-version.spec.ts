// "Version N" on a pinned workflow step's conversation (workflow-designer §23.5, PR-D.8c): beside
// the first message's "Context from" line, from the link the step was started with, the replay's
// `spawnedFrom.agentVersion`, or the source's run once it is loaded. Never with the flag off.
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { provideMarkdown } from 'ngx-markdown';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { environment } from '../../../../environments/environment';
import { FEATURES } from '../../../services/features';
import { LocalSettingsService } from '../../../services/local-settings.service';
import { HandoffStateService } from '../../services/handoff/handoff-state.service';
import { WorkflowRunStateService } from '../../services/handoff/workflow-run-state.service';
import { WorkflowRunView } from '../../services/handoff/workflow-run.models';
import { Message } from '../../services/models/message.model';
import { UserMessageComponent } from '../message-list/components/user-message.component';

const FIRST: Message = { id: 'msg-tgt-0', role: 'user', content: [{ type: 'text', text: 'Review this proposal' }] } as Message;

function run(agentVersion?: number): WorkflowRunView {
  return {
    runId: 'run1',
    sessionId: 'src',
    status: 'approved',
    request: 'Review this proposal',
    steps: [
      {
        stepId: 's1',
        handoffId: 'h1',
        agentId: 'a1',
        agentName: 'Generalist',
        request: 'Review this proposal',
        removed: false,
        stepName: 'generalist',
        ...(agentVersion !== undefined ? { agentVersion } : {}),
      },
    ],
    fileIds: [],
    limits: { maxSteps: 8, concurrency: 4 },
    sourceMessageIndex: -1,
    createdAt: '2026-11-22T00:00:00Z',
    updatedAt: '2026-11-22T00:00:00Z',
    workflow: { skillId: 'skill-1', name: 'review', title: 'Proposal review', fingerprint: 'fp' },
  };
}

function configure(agentVersionPinning: boolean): void {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  TestBed.configureTestingModule({
    providers: [
      provideHttpClient(),
      provideHttpClientTesting(),
      provideRouter([]),
      provideMarkdown(),
      { provide: FEATURES, useValue: { ...environment.features, handoffs: true, agentVersionPinning } },
      { provide: LocalSettingsService, useValue: { showDebugOutput: signal(false) } },
    ],
  });
}

function rendered(): string {
  const fixture = TestBed.createComponent(UserMessageComponent);
  fixture.componentRef.setInput('message', FIRST);
  fixture.detectChanges();
  return ((fixture.nativeElement as HTMLElement).textContent ?? '').replace(/\s+/g, ' ').trim();
}

const LINK = { sessionId: 'src', handoffId: 'h1', sourceTitle: 'Proposal', agentName: 'Generalist' };

describe('a pinned step’s conversation', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    TestBed.resetTestingModule();
  });

  describe('with version pinning on', () => {
    beforeEach(() => configure(true));

    it('says which version the step runs, from the link it was started with', () => {
      TestBed.inject(HandoffStateService).setSpawnedFrom('tgt', { ...LINK, agentVersion: 3 });
      const line = rendered();
      expect(line).toContain('Context from Proposal');
      expect(line).toContain('Version 3');
    });

    it('reads it from the source’s run when the link has none', () => {
      TestBed.inject(HandoffStateService).setSpawnedFrom('tgt', LINK);
      TestBed.inject(WorkflowRunStateService).hydrate('src', [run(4)]);
      expect(rendered()).toContain('Version 4');
    });

    it('says nothing for an unpinned step', () => {
      TestBed.inject(HandoffStateService).setSpawnedFrom('tgt', LINK);
      TestBed.inject(WorkflowRunStateService).hydrate('src', [run()]);
      expect(rendered()).toContain('Context from Proposal');
      expect(rendered()).not.toContain('Version');
    });

    it('keeps the version this tab sent when a replay without one replaces the link', () => {
      const state = TestBed.inject(HandoffStateService);
      state.setSpawnedFrom('tgt', { ...LINK, agentVersion: 3 });
      state.hydrate('tgt', [], LINK);
      expect(state.spawnedFrom('tgt')?.agentVersion).toBe(3);
      state.hydrate('tgt', [], { ...LINK, agentVersion: 5 });
      expect(state.spawnedFrom('tgt')?.agentVersion).toBe(5);
    });
  });

  describe('with version pinning off', () => {
    beforeEach(() => configure(false));

    it('shows the Context from line as today', () => {
      TestBed.inject(HandoffStateService).setSpawnedFrom('tgt', { ...LINK, agentVersion: 3 });
      expect(rendered()).toContain('Context from Proposal');
      expect(rendered()).not.toContain('Version');
    });
  });
});
