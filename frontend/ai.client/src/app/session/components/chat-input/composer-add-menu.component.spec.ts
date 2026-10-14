import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { Router } from '@angular/router';
import { of } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { FileSourceService } from '../../../assistants/services/file-source.service';
import { FileSourceConnector } from '../../../assistants/models/file-source.model';
import { ConnectorStatusService } from '../../../settings/connectors/services/connector-status.service';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import { UserConnectorsService } from '../../../settings/connectors/services/user-connectors.service';
import { SkillCommand, SkillCommandService } from '../../../services/skill/skill-command.service';
import { ToastService } from '../../../services/toast/toast.service';
import { Tool, ToolService } from '../../../services/tool/tool.service';
import { ComposerAddMenuComponent } from './composer-add-menu.component';

const DRIVE: FileSourceConnector = {
  providerId: 'google',
  displayName: 'Google Drive',
  iconName: 'heroCloud',
  connected: true,
};

const SKILLS: SkillCommand[] = [
  { skillId: 's1', slug: 'brand-deck', name: 'Brand Deck', description: 'Build a deck' },
  { skillId: 's2', slug: 'web-research', name: 'Web Research', description: 'Search the web' },
];

function tool(over: Partial<Tool>): Tool {
  return {
    toolId: 't',
    displayName: 'Tool',
    description: '',
    category: 'utility',
    icon: null,
    protocol: 'local',
    status: 'active',
    grantedBy: [],
    enabledByDefault: true,
    userEnabled: null,
    isEnabled: true,
    ...over,
  };
}

describe('ComposerAddMenuComponent', () => {
  let fixture: ComponentFixture<ComposerAddMenuComponent>;
  let component: ComposerAddMenuComponent;
  let listFileSources: ReturnType<typeof vi.fn>;
  let dialogOpen: ReturnType<typeof vi.fn>;
  let dialogResult: unknown;
  let navigate: ReturnType<typeof vi.fn>;
  let toggleTool: ReturnType<typeof vi.fn>;
  let ensureStatus: ReturnType<typeof vi.fn>;
  let markConnected: ReturnType<typeof vi.fn>;
  let markDisconnected: ReturnType<typeof vi.fn>;
  let initiateConsent: ReturnType<typeof vi.fn>;
  let disconnect: ReturnType<typeof vi.fn>;
  let completion: ReturnType<typeof signal<unknown>>;
  let inFlightProviders: ReturnType<typeof signal<Set<string>>>;
  let consent: {
    startUserConsent: ReturnType<typeof vi.fn>;
    openConsentPopup: ReturnType<typeof vi.fn>;
    isBlocked: ReturnType<typeof vi.fn>;
    acknowledgeCompletion: ReturnType<typeof vi.fn>;
  };
  let connectorsLoading: ReturnType<typeof signal<boolean>>;
  let connectorList: ReturnType<typeof signal<unknown[] | undefined>>;
  let agentLocked: ReturnType<typeof signal<boolean>>;
  let tools: ReturnType<typeof signal<Tool[]>>;
  let states: Record<string, 'connected' | 'disconnected' | 'unknown'>;

  beforeEach(async () => {
    listFileSources = vi.fn(async () => [DRIVE]);
    dialogResult = undefined;
    dialogOpen = vi.fn(() => ({ closed: of(dialogResult) }));
    navigate = vi.fn(async () => true);
    toggleTool = vi.fn(async () => undefined);
    ensureStatus = vi.fn(async () => undefined);
    markConnected = vi.fn((id: string) => { states[id] = 'connected'; });
    markDisconnected = vi.fn((id: string) => { states[id] = 'disconnected'; });
    initiateConsent = vi.fn(async () => ({ connected: false, authorizationUrl: 'https://auth.example/x' }));
    disconnect = vi.fn(async () => undefined);
    completion = signal<unknown>(null);
    inFlightProviders = signal(new Set<string>());
    // The real service marks the provider in flight while its popup is open.
    const openPopup = vi.fn(async (id: string) => {
      inFlightProviders.update((set) => new Set(set).add(id));
      return true;
    });
    consent = {
      startUserConsent: vi.fn(async (id: string) => openPopup(id)),
      openConsentPopup: openPopup,
      isBlocked: vi.fn(() => false),
      acknowledgeCompletion: vi.fn(),
    };
    connectorsLoading = signal(false);
    connectorList = signal<unknown[] | undefined>([
      { providerId: 'google', displayName: 'Google', providerType: 'google', iconName: 'x', scopes: [] },
      { providerId: 'slack', displayName: 'Slack', providerType: 'slack', iconName: 'x', scopes: [] },
    ]);
    agentLocked = signal(false);
    tools = signal<Tool[]>([
      tool({ toolId: 'web_search', displayName: 'Web Search', isEnabled: true }),
      tool({ toolId: 'analyze_spreadsheet', displayName: 'Spreadsheet Analysis', isEnabled: false }),
      tool({ toolId: 'pinned', displayName: 'Always On', isEnabled: true, alwaysOn: true }),
    ]);
    states = { google: 'connected', slack: 'disconnected' };

    await TestBed.configureTestingModule({
      imports: [ComposerAddMenuComponent],
      providers: [
        { provide: Dialog, useValue: { open: dialogOpen } },
        { provide: Router, useValue: { navigate } },
        { provide: ToastService, useValue: { error: vi.fn(), warning: vi.fn(), info: vi.fn(), success: vi.fn() } },
        { provide: FileSourceService, useValue: { listFileSources } },
        { provide: SkillCommandService, useValue: { commands: signal(SKILLS), load: vi.fn(async () => undefined) } },
        {
          provide: ToolService,
          useValue: {
            visibleTools: tools,
            agentLocked,
            isToolShownEnabled: (t: Tool) => t.isEnabled,
            toggleTool,
            ensureLoaded: vi.fn(async () => undefined),
          },
        },
        {
          provide: UserConnectorsService,
          useValue: {
            connectorsResource: { value: connectorList, isLoading: connectorsLoading },
            ensureLoaded: vi.fn(),
            initiateConsent,
            disconnect,
          },
        },
        {
          provide: ConnectorStatusService,
          useValue: {
            ensure: ensureStatus,
            stateFor: (id: string) => states[id] ?? 'unknown',
            isKnown: (id: string) => id in states,
            markConnected,
            markDisconnected,
          },
        },
        {
          provide: OAuthConsentService,
          useValue: { completion, inFlightProviders, ...consent },
        },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(ComposerAddMenuComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  afterEach(() => {
    document.querySelectorAll('.cdk-overlay-container').forEach((el) => el.remove());
  });

  const trigger = () =>
    fixture.nativeElement.querySelector('[data-testid="composer-add-trigger"]') as HTMLButtonElement;
  const menu = () => document.querySelector('[data-testid="composer-add-menu"]') as HTMLElement | null;
  const overlay = (testId: string) => document.querySelector(`[data-testid="${testId}"]`) as HTMLElement | null;

  async function settle(): Promise<void> {
    fixture.detectChanges();
    await fixture.whenStable();
    await new Promise((resolve) => setTimeout(resolve, 0));
    fixture.detectChanges();
  }

  async function openMenu(): Promise<void> {
    trigger().click();
    await settle();
  }

  async function openSubmenu(testId: string): Promise<void> {
    (menu()!.querySelector(`[data-testid="${testId}"]`) as HTMLButtonElement).click();
    await settle();
  }

  it('loads the file sources on open, not on render, and lists one row per source', async () => {
    expect(listFileSources).not.toHaveBeenCalled();
    expect(trigger().getAttribute('aria-haspopup')).toBe('menu');

    await openMenu();

    expect(listFileSources).toHaveBeenCalledTimes(1);
    expect(trigger().getAttribute('aria-expanded')).toBe('true');
    expect(menu()!.querySelector('[data-testid="composer-add-device"]')!.textContent).toContain('Add files or photos');
    expect(menu()!.querySelector('[data-testid="composer-add-source-google"]')!.textContent).toContain(
      'Add from Google Drive',
    );
  });

  it('shows no source row when the catalog is empty or cannot be read', async () => {
    listFileSources.mockRejectedValueOnce(new Error('503'));
    await openMenu();
    expect(menu()!.querySelector('[data-testid^="composer-add-source-"]')).toBeNull();
    expect(menu()!.querySelector('[data-testid="composer-add-device"]')).not.toBeNull();
  });

  it('"Add files or photos" hands the device picker back to the composer', async () => {
    const picked = vi.fn();
    component.pickFromDevice.subscribe(picked);
    await openMenu();

    (menu()!.querySelector('[data-testid="composer-add-device"]') as HTMLButtonElement).click();

    expect(picked).toHaveBeenCalledTimes(1);
  });

  it('"Add from Google Drive" opens the browser in attach mode and emits the pick', async () => {
    const picked = vi.fn();
    component.sourceFilesPicked.subscribe(picked);
    fixture.componentRef.setInput('remainingFiles', 3);
    dialogResult = { connector: DRIVE, files: [{ fileId: 'f1', name: 'notes.pdf' }] };
    await openMenu();

    (menu()!.querySelector('[data-testid="composer-add-source-google"]') as HTMLButtonElement).click();
    await settle();

    expect(dialogOpen).toHaveBeenCalledTimes(1);
    expect(dialogOpen.mock.calls[0][1]).toEqual({
      data: { connector: DRIVE, mode: 'attach', maxFiles: 3 },
    });
    expect(picked).toHaveBeenCalledWith(dialogResult);
    // The catalog is re-read afterwards: consent may have landed in the dialog.
    expect(listFileSources).toHaveBeenCalledTimes(2);
  });

  it('a cancelled browser emits nothing', async () => {
    const picked = vi.fn();
    component.sourceFilesPicked.subscribe(picked);
    await openMenu();

    (menu()!.querySelector('[data-testid="composer-add-source-google"]') as HTMLButtonElement).click();
    await settle();

    expect(picked).not.toHaveBeenCalled();
  });

  it('refuses both file rows once the message is full', async () => {
    const toast = TestBed.inject(ToastService);
    const picked = vi.fn();
    component.pickFromDevice.subscribe(picked);
    fixture.componentRef.setInput('remainingFiles', 0);
    await openMenu();

    (menu()!.querySelector('[data-testid="composer-add-device"]') as HTMLButtonElement).click();

    expect(picked).not.toHaveBeenCalled();
    expect(dialogOpen).not.toHaveBeenCalled();
    expect(toast.warning).toHaveBeenCalledWith('File limit', expect.stringContaining('Maximum 5 files'));
  });

  it('lists the skills that are turned on and emits the one picked', async () => {
    const picked = vi.fn();
    component.skillPicked.subscribe(picked);
    await openMenu();
    await openSubmenu('composer-add-skills');

    const skills = overlay('composer-skills-menu')!;
    expect(skills.textContent).toContain('Brand Deck');
    expect(skills.textContent).toContain('/web-research');

    (skills.querySelector('[data-skill-id="s2"]') as HTMLButtonElement).click();

    expect(picked).toHaveBeenCalledWith(SKILLS[1]);
  });

  it('"Manage skills" goes to Customize → Skills', async () => {
    await openMenu();
    await openSubmenu('composer-add-skills');

    (overlay('composer-skills-menu')!.querySelector('[data-testid="composer-manage-skills"]') as HTMLButtonElement).click();

    expect(navigate).toHaveBeenCalledWith(['/customize/skills']);
  });

  it('hides the Skills row where the composer hides `/` commands', async () => {
    fixture.componentRef.setInput('showSkills', false);
    await openMenu();
    expect(menu()!.querySelector('[data-testid="composer-add-skills"]')).toBeNull();
    expect(menu()!.querySelector('[data-testid="composer-add-tools"]')).not.toBeNull();
  });

  it('lists tools by name as switches, flips one through the tool service and stays open', async () => {
    await openMenu();
    await openSubmenu('composer-add-tools');

    const toolsMenu = overlay('composer-tools-menu')!;
    const names = Array.from(toolsMenu.querySelectorAll('[data-tool-id]')).map((el) =>
      el.textContent!.trim(),
    );
    expect(names).toEqual(['Always On', 'Spreadsheet Analysis', 'Web Search']);
    const row = toolsMenu.querySelector('[data-tool-id="analyze_spreadsheet"]') as HTMLButtonElement;
    expect(row.getAttribute('role')).toBe('menuitemcheckbox');
    expect(row.getAttribute('aria-checked')).toBe('false');
    expect(toolsMenu.querySelector('[data-tool-id="web_search"]')!.getAttribute('aria-checked')).toBe('true');

    row.click();
    await settle();

    expect(toggleTool).toHaveBeenCalledWith('analyze_spreadsheet');
    expect(overlay('composer-tools-menu')).not.toBeNull();
    expect(menu()).not.toBeNull();
  });

  it('locks a pinned tool, and every tool while an Agent dictates the set', async () => {
    await openMenu();
    await openSubmenu('composer-add-tools');
    const toolsMenu = overlay('composer-tools-menu')!;
    expect(toolsMenu.querySelector('[data-tool-id="pinned"]')!.getAttribute('aria-disabled')).toBe('true');
    expect(toolsMenu.querySelector('[data-tool-id="web_search"]')!.getAttribute('aria-disabled')).toBeNull();

    (toolsMenu.querySelector('[data-tool-id="pinned"]') as HTMLButtonElement).click();
    expect(toggleTool).not.toHaveBeenCalled();

    agentLocked.set(true);
    await settle();
    expect(toolsMenu.textContent).toContain("This conversation's agent chooses its tools");
    expect(toolsMenu.querySelector('[data-tool-id="web_search"]')!.getAttribute('aria-disabled')).toBe('true');
  });

  it('lists connectors as switches with their connection state and probes the unknown ones', async () => {
    await openMenu();
    await openSubmenu('composer-add-connectors');

    expect(ensureStatus).toHaveBeenCalledWith(['google', 'slack']);
    const connectors = overlay('composer-connectors-menu')!;
    const google = connectors.querySelector('[data-connector-id="google"]')!;
    const slack = connectors.querySelector('[data-connector-id="slack"]')!;
    expect(google.getAttribute('role')).toBe('menuitemcheckbox');
    expect(google.getAttribute('aria-checked')).toBe('true');
    expect(google.textContent).toContain('Connected');
    expect(slack.getAttribute('aria-checked')).toBe('false');
    expect(slack.textContent).toContain('Not connected');
  });

  it('switching a connector on runs the consent popup, and the completion settles it', async () => {
    await openMenu();
    await openSubmenu('composer-add-connectors');
    const row = () => overlay('composer-connectors-menu')!.querySelector('[data-connector-id="slack"]') as HTMLButtonElement;

    row().click();
    await settle();

    expect(initiateConsent).toHaveBeenCalledWith('slack');
    // User-initiated, so the conversation draws no consent card for it.
    expect(consent.startUserConsent).toHaveBeenCalledWith('slack', 'https://auth.example/x');
    expect(row().textContent).toContain('Awaiting consent');
    expect(row().getAttribute('aria-disabled')).toBe('true');
    // The menu stays open through it.
    expect(menu()).not.toBeNull();

    completion.set({ providerId: 'slack', status: 'success' });
    await settle();

    expect(consent.acknowledgeCompletion).toHaveBeenCalled();
    expect(row().getAttribute('aria-disabled')).toBeNull();
    expect(row().textContent).not.toContain('Awaiting consent');
    expect(TestBed.inject(ToastService).success).toHaveBeenCalledWith('Slack connected.');
  });

  it('a blocked popup offers to open the sign-in again from a click', async () => {
    consent.startUserConsent.mockResolvedValueOnce(false);
    consent.isBlocked.mockReturnValue(true);
    await openMenu();
    await openSubmenu('composer-add-connectors');
    const connectorsMenu = () => overlay('composer-connectors-menu')!;
    const row = () => connectorsMenu().querySelector('[data-connector-id="slack"]') as HTMLButtonElement;

    row().click();
    await settle();

    expect(row().textContent).toContain('Pop-up blocked');
    // Not busy: flipping it again retries from the start.
    expect(row().getAttribute('aria-disabled')).toBeNull();
    const retry = connectorsMenu().querySelector('[data-retry-connector-id="slack"]') as HTMLButtonElement;
    expect(retry.getAttribute('aria-label')).toBe('Open sign-in for Slack');

    retry.click();
    await settle();

    expect(consent.openConsentPopup).toHaveBeenCalledWith('slack');
    // The menu stays open, and the row reads as any sign-in in flight.
    expect(row().textContent).toContain('Awaiting consent');
    expect(connectorsMenu().querySelector('[data-retry-connector-id="slack"]')).toBeNull();
    // A completion while blocked still settles the row and says so.
    completion.set({ providerId: 'slack', status: 'success' });
    await settle();
    expect(consent.acknowledgeCompletion).toHaveBeenCalled();
    expect(TestBed.inject(ToastService).success).toHaveBeenCalledWith('Slack connected.');
  });

  it('draws placeholder rows while the connector list loads', async () => {
    connectorList.set(undefined);
    connectorsLoading.set(true);
    await openMenu();
    await openSubmenu('composer-add-connectors');

    const connectorsMenu = overlay('composer-connectors-menu')!;
    const loading = connectorsMenu.querySelector('[data-testid="composer-connectors-loading"]')!;
    expect(loading.getAttribute('role')).toBe('status');
    expect(loading.textContent).toContain('Loading connectors');
    expect(connectorsMenu.textContent).not.toContain('No connectors available');
  });

  it('a row whose status has not been probed yet is a skeleton, not a switch', async () => {
    delete states['slack'];
    await openMenu();
    await openSubmenu('composer-add-connectors');
    const row = overlay('composer-connectors-menu')!.querySelector('[data-connector-id="slack"]') as HTMLButtonElement;

    expect(row.querySelector('[data-testid="connector-state-skeleton"]')).not.toBeNull();
    expect(row.textContent).toContain('Checking connection');
    expect(row.getAttribute('aria-busy')).toBe('true');
    expect(row.getAttribute('aria-disabled')).toBe('true');

    row.click();
    await settle();
    expect(initiateConsent).not.toHaveBeenCalled();
  });

  it('a consent already vaulted switches on without a popup', async () => {
    initiateConsent.mockResolvedValueOnce({ connected: true, authorizationUrl: null });
    await openMenu();
    await openSubmenu('composer-add-connectors');

    (overlay('composer-connectors-menu')!.querySelector('[data-connector-id="slack"]') as HTMLButtonElement).click();
    await settle();

    expect(consent.startUserConsent).not.toHaveBeenCalled();
    expect(markConnected).toHaveBeenCalledWith('slack');
  });

  it('a popup closed without finishing puts the switch back', async () => {
    await openMenu();
    await openSubmenu('composer-add-connectors');
    const row = () => overlay('composer-connectors-menu')!.querySelector('[data-connector-id="slack"]') as HTMLButtonElement;
    row().click();
    await settle();
    expect(row().textContent).toContain('Awaiting consent');

    inFlightProviders.set(new Set());
    await settle();

    expect(row().textContent).toContain('Not connected');
    expect(row().getAttribute('aria-disabled')).toBeNull();
  });

  it('switching a connector off disconnects it and keeps the menu open', async () => {
    const toast = TestBed.inject(ToastService);
    await openMenu();
    await openSubmenu('composer-add-connectors');
    const row = () => overlay('composer-connectors-menu')!.querySelector('[data-connector-id="google"]') as HTMLButtonElement;

    row().click();
    await settle();

    expect(disconnect).toHaveBeenCalledWith('google');
    expect(markDisconnected).toHaveBeenCalledWith('google');
    expect(toast.success).toHaveBeenCalledWith('Google disconnected.');
    expect(row().getAttribute('aria-checked')).toBe('false');
    expect(row().textContent).toContain('Not connected');
    expect(overlay('composer-connectors-menu')).not.toBeNull();
  });

  it('"Manage connectors" still goes to Customize → Connectors', async () => {
    await openMenu();
    await openSubmenu('composer-add-connectors');
    (overlay('composer-connectors-menu')!.querySelector('[data-testid="composer-manage-connectors"]') as HTMLButtonElement).click();
    expect(navigate).toHaveBeenCalledWith(['/customize/connectors']);
  });
});
