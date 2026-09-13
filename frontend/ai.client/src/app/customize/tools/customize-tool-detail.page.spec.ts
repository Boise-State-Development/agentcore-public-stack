import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { signal } from '@angular/core';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { CustomizeToolDetailPage } from './customize-tool-detail.page';
import { Tool, ToolService, ToolsResponse } from '../../services/tool/tool.service';
import { ConfigService } from '../../services/config.service';
import {
  ConnectionState,
  ConnectorStatusService,
} from '../../settings/connectors/services/connector-status.service';
import { OAuthConsentService } from '../../services/oauth-consent/oauth-consent.service';

/** See the twin in `customize-tools.page.spec.ts`: the real one owns a BroadcastChannel. */
class FakeConnectorStatus {
  states: Record<string, ConnectionState> = {};
  ensured: string[] = [];

  stateFor(providerId: string | null | undefined): ConnectionState {
    return providerId ? (this.states[providerId] ?? 'unknown') : 'unknown';
  }

  async ensure(providerIds: readonly (string | null | undefined)[]): Promise<void> {
    this.ensured.push(...providerIds.filter((p): p is string => !!p));
  }
}

/**
 * Stands in for the real consent service, which reaches `UserConnectorsService`
 * and `SessionService` — both of which fetch in their constructors. Left real,
 * those requests are never flushed, the app never reaches stability, and every
 * `whenStable()` in this file times out. The page asks it three things.
 */
class FakeConsent {
  readonly opened: string[] = [];
  private readonly inFlight = signal<ReadonlySet<string>>(new Set());
  readonly inFlightProviders = this.inFlight.asReadonly();

  requestConsent(): void {}

  async openConsentPopup(providerId: string): Promise<boolean> {
    this.opened.push(providerId);
    return true;
  }
}

/** A failed save lands a microtask after `whenStable()`; one macrotask makes it observable. */
const settle = () => new Promise(resolve => setTimeout(resolve, 0));

const tool = (overrides: Partial<Tool> & Pick<Tool, 'toolId' | 'displayName'>): Tool => ({
  description: 'Does a thing.',
  category: 'utility',
  icon: null,
  protocol: 'local',
  status: 'active',
  grantedBy: [],
  enabledByDefault: true,
  userEnabled: null,
  isEnabled: true,
  ...overrides,
});

const CATALOG: Tool[] = [
  tool({ toolId: 'web_search', displayName: 'Web Search', category: 'search' }),
  tool({
    toolId: 'canvas_faculty',
    displayName: 'Canvas Faculty',
    category: 'data',
    protocol: 'mcp_external',
    description: 'Canvas LMS access.\n\nArgs:\n  course_id: the course',
    requiresOauthProvider: 'canvas',
    serverTools: [
      { name: 'list_courses', description: 'List your courses.', enabled: true },
      { name: 'list_assignments', description: 'List assignments.', enabled: false },
    ],
  }),
];

const SNAPSHOT = {
  toolId: 'canvas_faculty',
  prompts: [
    { name: 'grade_summary', title: 'Grade summary', description: 'Summarize grades.', arguments: ['course_id'] },
  ],
  resources: [
    {
      uri: 'canvas://courses/{course_id}/syllabus',
      name: 'Syllabus',
      description: null,
      mimeType: 'text/html',
      uriTemplate: true,
    },
  ],
  supportsPrompts: true,
  supportsResources: true,
  discoveredAt: '2026-09-01T00:00:00Z',
  discoveredBy: 'admin',
  error: null,
  truncated: false,
};

describe('CustomizeToolDetailPage', () => {
  let http: HttpTestingController;
  let tools: ToolService;
  let connectors: FakeConnectorStatus;
  let consent: FakeConsent;

  beforeEach(() => {
    TestBed.resetTestingModule();
    connectors = new FakeConnectorStatus();
    consent = new FakeConsent();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: ConnectorStatusService, useValue: connectors },
        { provide: OAuthConsentService, useValue: consent },
      ],
    });
    TestBed.inject(ConfigService).appApiUrl.set('/api');
    http = TestBed.inject(HttpTestingController);

    tools = TestBed.inject(ToolService);
    const response: ToolsResponse = {
      tools: CATALOG.map(t => ({ ...t, serverTools: t.serverTools?.map(s => ({ ...s })) })),
      categories: ['search', 'data'],
      appRolesApplied: [],
    };
    http.match(req => req.url.startsWith('/api/tools')).forEach(req => req.flush(response));
  });

  afterEach(() => {
    http.verify();
    TestBed.resetTestingModule();
  });

  async function create(toolId: string): Promise<ComponentFixture<CustomizeToolDetailPage>> {
    const fixture = TestBed.createComponent(CustomizeToolDetailPage);
    fixture.componentRef.setInput('toolId', toolId);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  const text = (fixture: ComponentFixture<CustomizeToolDetailPage>) =>
    (fixture.nativeElement as HTMLElement).textContent ?? '';

  it('shows the tool it was routed to', async () => {
    const fixture = await create('web_search');
    expect(text(fixture)).toContain('Web Search');
    expect(text(fixture)).toContain('web_search');
  });

  it('says so when the id is not in the catalog', async () => {
    const fixture = await create('not_a_tool');
    expect(text(fixture)).toContain('Tool not found');
  });

  it('lists an MCP server’s tools with their own switches', async () => {
    const fixture = await create('canvas_faculty');
    http.expectOne('/api/tools/canvas_faculty/capabilities').flush(SNAPSHOT);
    expect(text(fixture)).toContain('list_courses');
    expect(text(fixture)).toContain('list_assignments');
    // Master switch + one per sub-tool.
    expect(fixture.nativeElement.querySelectorAll('button[role="switch"]')).toHaveLength(3);
  });

  it('keeps the docstring reference material out of the prose', async () => {
    const fixture = await create('canvas_faculty');
    http.expectOne('/api/tools/canvas_faculty/capabilities').flush(SNAPSHOT);
    expect(text(fixture)).toContain('Canvas LMS access.');
    expect(text(fixture)).not.toContain('course_id: the course');

    fixture.componentInstance['showDescriptionDetail'].set(true);
    fixture.detectChanges();
    expect(text(fixture)).toContain('course_id: the course');
  });

  it('reads prompts and resources from the stored snapshot', async () => {
    const fixture = await create('canvas_faculty');
    http.expectOne('/api/tools/canvas_faculty/capabilities').flush(SNAPSHOT);
    await fixture.whenStable();
    fixture.detectChanges();

    expect(text(fixture)).toContain('Grade summary');
    expect(text(fixture)).toContain('arguments: course_id');
    expect(text(fixture)).toContain('canvas://courses/{course_id}/syllabus');
    expect(text(fixture)).toContain('template');
  });

  it('asks for no capability snapshot for a tool that is not an external MCP server', async () => {
    // A local tool has no server to have been asked; the request would 404 or,
    // worse, succeed with an empty snapshot and read as "offers nothing".
    await create('web_search');
    http.expectNone('/api/tools/web_search/capabilities');
  });

  it('writes a sub-tool toggle through to the preferences endpoint', async () => {
    const fixture = await create('canvas_faculty');
    http.expectOne('/api/tools/canvas_faculty/capabilities').flush(SNAPSHOT);
    fixture.detectChanges();

    const switches = fixture.nativeElement.querySelectorAll(
      'button[role="switch"]',
    ) as NodeListOf<HTMLButtonElement>;
    switches[2].click(); // list_assignments, currently off
    await fixture.whenStable();

    const req = http.expectOne('/api/tools/preferences');
    expect(req.request.body.preferences).toEqual({ 'canvas_faculty::list_assignments': true });
    req.flush({});
  });

  it('surfaces a failed save instead of letting the switch snap back silently', async () => {
    const fixture = await create('web_search');
    const toggle = fixture.nativeElement.querySelector(
      'button[role="switch"]',
    ) as HTMLButtonElement;
    toggle.click();
    await fixture.whenStable();

    http.expectOne('/api/tools/preferences').flush('nope', { status: 500, statusText: 'Error' });
    await settle();
    fixture.detectChanges();

    expect(text(fixture)).toContain("Couldn't save the change to Web Search");
    expect(tools.getTool('web_search')?.isEnabled).toBe(true);
  });

  describe('the agent-lock seam', () => {
    // Same reasoning as the list page: the lock is conversation-scoped state on
    // a root singleton, and a global preference page must ignore it entirely.
    // See docs/specs/customize-surface.md §"The agent-lock seam".

    it('still saves a toggle while a lock is held', async () => {
      tools.lockToAgentTools(['canvas_faculty']);
      const fixture = await create('web_search');

      const toggle = fixture.nativeElement.querySelector(
        'button[role="switch"]',
      ) as HTMLButtonElement;
      toggle.click();
      await fixture.whenStable();

      const req = http.expectOne('/api/tools/preferences');
      expect(req.request.body.preferences).toEqual({ web_search: false });
      req.flush({});
    });

    it('shows the user’s own enabled state, not membership of the bound set', async () => {
      tools['_tools'].update(list =>
        list.map(t => (t.toolId === 'web_search' ? { ...t, isEnabled: false } : t)),
      );
      tools.lockToAgentTools(['web_search']);
      const fixture = await create('web_search');

      expect(tools.isToolShownEnabled(tools.getTool('web_search')!)).toBe(true); // drawer shim
      expect(text(fixture)).toContain('Off');
    });
  });
});
