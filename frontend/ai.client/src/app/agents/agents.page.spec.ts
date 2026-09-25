import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { provideRouter } from '@angular/router';
import { AgentsPage } from './agents.page';
import { AgentService } from './services/agent.service';
import { Agent } from './models/agent.model';
import { LocalSettingsService } from '../services/local-settings.service';
import { ConfigService } from '../services/config.service';

const AGENT = {
  agentId: 'ast-a1b2c3d4e5f6',
  name: 'Rubric Builder',
  description: 'Builds rubrics',
  visibility: 'PRIVATE',
  status: 'COMPLETE',
  userPermission: 'owner',
} as unknown as Agent;

describe('AgentsPage row actions', () => {
  const viewMode = signal<'grid' | 'list'>('grid');

  beforeEach(() => {
    TestBed.resetTestingModule();
    viewMode.set('grid');
    TestBed.configureTestingModule({
      imports: [AgentsPage],
      providers: [
        provideRouter([]),
        {
          provide: AgentService,
          useValue: {
            agents$: signal([AGENT]),
            loading$: signal(false),
            error$: signal(null),
            loadAgents: vi.fn().mockResolvedValue(undefined),
          },
        },
        {
          provide: LocalSettingsService,
          useValue: { agentsViewMode: viewMode, setAgentsViewMode: (v: 'grid' | 'list') => viewMode.set(v) },
        },
        { provide: ConfigService, useValue: { appApiUrl: () => '' } },
      ],
    });
  });

  // Icon-only buttons: the tooltip only describes them (aria-describedby), so without
  // a label a screen reader announced every Chat, Edit, Share and Delete as "button".
  it.each(['grid', 'list'] as const)('names every icon-only action after its agent (%s view)', mode => {
    viewMode.set(mode);
    const fixture = TestBed.createComponent(AgentsPage);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;

    const labels = Array.from(el.querySelectorAll('button'))
      .filter(b => b.querySelector('ng-icon') && !b.textContent?.trim())
      .map(b => b.getAttribute('aria-label'));

    expect(labels).toEqual([
      'Chat with Rubric Builder',
      'Edit Rubric Builder',
      'Share Rubric Builder',
      'Delete Rubric Builder',
    ]);
  });
});
