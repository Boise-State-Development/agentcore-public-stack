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

  async startUserConsent(providerId: string): Promise<boolean> {
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
    {
      name: 'grade_summary',
      title: 'Grade summary',
      description: 'Summarize grades.',
      arguments: [
        { name: 'course_id', description: 'Which course', required: true },
        { name: 'tone', description: null, required: false },
      ],
    },
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
    expect(text(fixture)).toContain('canvas://courses/{course_id}/syllabus');
    expect(text(fixture)).toContain('template');
  });

  // ===========================================================================
  // Trying a prompt
  // ===========================================================================

  /** Opens `canvas_faculty` with its snapshot flushed and one prompt expanded. */
  async function openPromptForm(): Promise<ComponentFixture<CustomizeToolDetailPage>> {
    const fixture = await create('canvas_faculty');
    http.expectOne('/api/tools/canvas_faculty/capabilities').flush(SNAPSHOT);
    await fixture.whenStable();
    fixture.detectChanges();

    fixture.componentInstance['togglePrompt'](SNAPSHOT.prompts[0] as never);
    fixture.detectChanges();
    return fixture;
  }

  const field = (fixture: ComponentFixture<CustomizeToolDetailPage>, name: string) =>
    (fixture.nativeElement as HTMLElement).querySelector<HTMLInputElement>(
      `#prompt-arg-grade_summary-${name}`,
    );

  it('still labels arguments from a backend that predates the structured shape', async () => {
    // The two packages deploy separately, so a SPA can reach an older backend
    // that sends bare names. A field labelled `undefined` would be the tell.
    const fixture = await create('canvas_faculty');
    http.expectOne('/api/tools/canvas_faculty/capabilities').flush({
      ...SNAPSHOT,
      prompts: [{ ...SNAPSHOT.prompts[0], arguments: ['course_id'] }],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    fixture.componentInstance['togglePrompt'](
      fixture.componentInstance['prompts']()[0] as never,
    );
    fixture.detectChanges();

    expect(field(fixture, 'course_id')).toBeTruthy();
    expect(text(fixture)).not.toContain('undefined');
  });

  it('builds a field per argument, marking the required ones', async () => {
    const fixture = await openPromptForm();

    expect(field(fixture, 'course_id')).toBeTruthy();
    expect(field(fixture, 'tone')).toBeTruthy();
    // The hint the server gave for the argument, not just its name.
    expect(text(fixture)).toContain('Which course');
    expect(text(fixture)).toContain('(required)');
  });

  it('refuses to compose while a required argument is blank, and says which', async () => {
    const fixture = await openPromptForm();

    fixture.componentInstance['resolvePrompt'](SNAPSHOT.prompts[0] as never);
    await settle();
    fixture.detectChanges();

    expect(text(fixture)).toContain('This one is required.');
    // Nothing was sent — the server would have answered with a developer's error.
    http.expectNone('/api/tools/canvas_faculty/prompts/grade_summary');
  });

  it('clears a field’s error as soon as it is edited', async () => {
    const fixture = await openPromptForm();
    fixture.componentInstance['resolvePrompt'](SNAPSHOT.prompts[0] as never);
    await settle();
    fixture.detectChanges();
    expect(text(fixture)).toContain('This one is required.');

    const input = field(fixture, 'course_id')!;
    input.value = 'BIO 101';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();

    expect(text(fixture)).not.toContain('This one is required.');
  });

  it('composes the prompt and shows what the server returned', async () => {
    const fixture = await openPromptForm();
    const input = field(fixture, 'course_id')!;
    input.value = 'BIO 101';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();

    const resolving = fixture.componentInstance['resolvePrompt'](
      SNAPSHOT.prompts[0] as never,
    );
    const request = http.expectOne('/api/tools/canvas_faculty/prompts/grade_summary');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({
      arguments: { course_id: 'BIO 101', tone: '' },
    });
    request.flush({
      description: 'Grades',
      messages: [
        { role: 'user', kind: 'text', text: 'Summarize grades for BIO 101.' },
        { role: 'user', kind: 'image', text: '' },
      ],
      truncated: false,
    });
    await resolving;
    fixture.detectChanges();

    expect(text(fixture)).toContain('Summarize grades for BIO 101.');
    // An image has no readable body, so the message is named rather than blank.
    expect(text(fixture)).toContain('image content — not shown here');
  });

  it('keeps a failed compose on the page instead of clearing the form', async () => {
    const fixture = await openPromptForm();
    const input = field(fixture, 'course_id')!;
    input.value = 'BIO 101';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();

    const resolving = fixture.componentInstance['resolvePrompt'](
      SNAPSHOT.prompts[0] as never,
    );
    http
      .expectOne('/api/tools/canvas_faculty/prompts/grade_summary')
      .flush({ detail: 'nope' }, { status: 502, statusText: 'Bad Gateway' });
    await resolving;
    fixture.detectChanges();

    expect(text(fixture)).toContain('couldn’t compose that prompt');
    expect(field(fixture, 'course_id')!.value).toBe('BIO 101');
  });

  it('discards a run when a different prompt is opened', async () => {
    const fixture = await openPromptForm();
    const input = field(fixture, 'course_id')!;
    input.value = 'BIO 101';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();

    // Closing and reopening is the same path as switching prompts.
    fixture.componentInstance['togglePrompt'](SNAPSHOT.prompts[0] as never);
    fixture.componentInstance['togglePrompt'](SNAPSHOT.prompts[0] as never);
    fixture.detectChanges();

    expect(field(fixture, 'course_id')!.value).toBe('');
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
