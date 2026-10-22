import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { CustomizeToolsPage } from './customize-tools.page';
import { Tool, ToolService, ToolsResponse } from '../../services/tool/tool.service';
import { ConfigService } from '../../services/config.service';
import {
  ConnectionState,
  ConnectorStatusService,
} from '../../settings/connectors/services/connector-status.service';

/**
 * Stands in for the real status service, which owns a BroadcastChannel and a
 * window `message` subscription this page has no interest in. The page only
 * ever asks it two things.
 */
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
 * The rejection from a failed save travels catch → signal set, which lands a
 * microtask after `whenStable()` has already resolved on the HTTP task. One
 * macrotask tick is what makes the banner observable.
 */
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
  tool({ toolId: 'calculator', displayName: 'Calculator', category: 'utility', isEnabled: false }),
  tool({
    toolId: 'canvas_faculty',
    displayName: 'Canvas Faculty',
    category: 'data',
    protocol: 'mcp_external',
    description: 'Canvas LMS access.\n\nArgs:\n  course_id: the course',
    serverTools: [
      { name: 'list_courses', enabled: true },
      { name: 'list_assignments', enabled: false },
    ],
  }),
];

describe('CustomizeToolsPage', () => {
  let http: HttpTestingController;
  let tools: ToolService;
  let connectors: FakeConnectorStatus;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    connectors = new FakeConnectorStatus();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: ConnectorStatusService, useValue: connectors },
      ],
    });
    TestBed.inject(ConfigService).appApiUrl.set('/api');
    http = TestBed.inject(HttpTestingController);

    // ToolService loads in its own constructor; settle that before the page mounts
    // so the component sees a resolved catalog.
    tools = TestBed.inject(ToolService);
    const response: ToolsResponse = {
      tools: CATALOG.map(t => ({ ...t })),
      categories: ['search', 'utility', 'data'],
      appRolesApplied: [],
    };
    http.match(req => req.url.startsWith('/api/tools')).forEach(req => req.flush(response));
  });

  afterEach(() => {
    http.verify();
    TestBed.resetTestingModule();
  });

  async function create() {
    const fixture = TestBed.createComponent(CustomizeToolsPage);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  const text = (fixture: { nativeElement: HTMLElement }) => fixture.nativeElement.textContent ?? '';

  it('lists every granted tool as a card', async () => {
    const fixture = await create();
    expect(fixture.nativeElement.querySelectorAll('app-customize-card')).toHaveLength(3);
    expect(text(fixture)).toContain('Web Search');
    expect(text(fixture)).toContain('Canvas Faculty');
  });

  it('links each card to its detail page', async () => {
    const fixture = await create();
    const links = [...fixture.nativeElement.querySelectorAll('app-customize-card a')].map(
      (a: Element) => a.getAttribute('href'),
    );
    expect(links).toEqual([
      '/customize/tools/web_search',
      '/customize/tools/calculator',
      '/customize/tools/canvas_faculty',
    ]);
  });

  it('keeps the switch out of the card link', async () => {
    // A control nested inside a link is a control the user cannot operate with
    // the keyboard without also following the link.
    const fixture = await create();
    const toggle = fixture.nativeElement.querySelector('app-customize-card button[role="switch"]');
    expect(toggle.closest('a')).toBeNull();
  });

  it('counts what is on, not what exists', async () => {
    const fixture = await create();
    expect(text(fixture)).toContain('2 of 3 tools on');
  });

  it('searches across name, description and category', async () => {
    const fixture = await create();
    fixture.componentInstance['query'].set('canvas');
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelectorAll('app-customize-card')).toHaveLength(1);
    expect(text(fixture)).toContain('Canvas Faculty');
  });

  it('filters by category chip', async () => {
    const fixture = await create();
    fixture.componentInstance['activeCategory'].set('search');
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelectorAll('app-customize-card')).toHaveLength(1);
    expect(text(fixture)).toContain('Web Search');
  });

  it('leads an MCP server card with its partial-selection state', async () => {
    const fixture = await create();
    // The count is the fact a card can't bury while there is no per-tool pane.
    expect(text(fixture)).toContain('1 of 2 tools on');
    // And the docstring Args: block never reaches the card.
    expect(text(fixture)).not.toContain('course_id');
  });

  it('counts a single-tool server in the singular', async () => {
    // The dev catalog has several of these (Hello World, Search Boise State),
    // and "1 tools" is the kind of thing a browse surface cannot ship with.
    tools['_tools'].set([
      tool({
        toolId: 'hello_world',
        displayName: 'Hello World',
        protocol: 'mcp',
        serverTools: [{ name: 'hello', enabled: true }],
      }),
    ]);
    const fixture = await create();
    expect(text(fixture)).toContain('1 tool ·');
    expect(text(fixture)).not.toContain('1 tools');
  });

  it('writes a toggle through to the preferences endpoint', async () => {
    const fixture = await create();
    const toggle = fixture.nativeElement.querySelector(
      'app-customize-card button[role="switch"]',
    ) as HTMLButtonElement;
    toggle.click();
    await fixture.whenStable();

    const req = http.expectOne('/api/tools/preferences');
    expect(req.request.body.preferences).toEqual({ web_search: false });
    req.flush({});
  });

  it('surfaces a failed save instead of letting the switch snap back silently', async () => {
    const fixture = await create();
    const toggle = fixture.nativeElement.querySelector(
      'app-customize-card button[role="switch"]',
    ) as HTMLButtonElement;
    toggle.click();
    await fixture.whenStable();

    http.expectOne('/api/tools/preferences').flush('nope', { status: 500, statusText: 'Error' });
    await settle();
    fixture.detectChanges();

    expect(text(fixture)).toContain("Couldn't save the change to Web Search");
    // The service reverted its optimistic update, so the switch is back ON.
    expect(tools.getTool('web_search')?.isEnabled).toBe(true);
  });

  describe('the agent-lock seam', () => {
    // ToolService is a root singleton and the session view never releases the
    // Agent binding lock on teardown, so a user can arrive here carrying one.
    // A global preference page must ignore it entirely.
    // See docs/specs/customize-surface.md §"The agent-lock seam".

    it('shows the user their own catalog, not the Agent’s bound subset', async () => {
      tools.lockToAgentTools(['canvas_faculty']);
      const fixture = await create();

      expect(tools.agentLocked()).toBe(true);
      expect(tools.visibleTools()).toHaveLength(1); // what the drawer would show
      expect(fixture.nativeElement.querySelectorAll('app-customize-card')).toHaveLength(3);
    });

    it('shows the user’s own enabled state, not membership of the bound set', async () => {
      // `calculator` is OFF for the user and absent from the Agent's bindings.
      // The drawer's shim would render it ON if it were bound, and the page
      // must not inherit that.
      tools.lockToAgentTools(['calculator']);
      const fixture = await create();

      expect(tools.isToolShownEnabled(CATALOG[1])).toBe(true); // drawer shim says ON
      const cards = fixture.componentInstance['cards']();
      expect(cards.find(c => c.tool.toolId === 'calculator')?.enabled).toBe(false);
    });

    it('still saves a toggle while a lock is held', async () => {
      tools.lockToAgentTools(['canvas_faculty']);
      const fixture = await create();

      const toggle = fixture.nativeElement.querySelector(
        'app-customize-card button[role="switch"]',
      ) as HTMLButtonElement;
      toggle.click();
      await fixture.whenStable();

      // Without `respectAgentLock: false` this request is never made and the
      // user's click vanishes with no feedback.
      const req = http.expectOne('/api/tools/preferences');
      expect(req.request.body.preferences).toEqual({ web_search: false });
      req.flush({});
    });
  });
});
