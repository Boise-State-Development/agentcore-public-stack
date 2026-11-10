import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import {
  AgentLaunchCardComponent,
  AgentLaunchCardView,
  agentLaunchCardView,
} from './agent-launch-card.component';
import { Agent, AgentRunnability } from '../models/agent.model';
import { gradientFor } from './agent-icon.component';
import { ConfigService } from '../../services/config.service';

describe('AgentLaunchCardComponent', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])],
    });
    TestBed.inject(ConfigService).appApiUrl.set('/api');
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => TestBed.resetTestingModule());

  const view = (overrides: Partial<AgentLaunchCardView> = {}): AgentLaunchCardView => ({
    agentId: 'ast-1',
    name: 'Bronco Advisor',
    starters: [],
    listed: false,
    pinnable: false,
    ...overrides,
  });

  function create(input: AgentLaunchCardView, runnability?: AgentRunnability | null) {
    const fixture = TestBed.createComponent(AgentLaunchCardComponent);
    fixture.componentRef.setInput('view', input);
    if (runnability !== undefined) fixture.componentRef.setInput('runnability', runnability);
    fixture.detectChanges();
    // A `pinnable` card loads the session-wide pin list on mount.
    http.match(`/api/agents/pins`).forEach((req) => req.flush({ pins: [] }));
    fixture.detectChanges();
    return fixture;
  }

  const text = (fixture: { nativeElement: HTMLElement }) =>
    fixture.nativeElement.textContent ?? '';

  /** The attribution line, span by span: `['By', 'A. Author', '·', 'Advising']`. */
  const attribution = (fixture: { nativeElement: HTMLElement }) =>
    Array.from(fixture.nativeElement.querySelectorAll('h2 ~ p:last-of-type span')).map((s) =>
      (s.textContent ?? '').trim(),
    );

  // ── identity (the reason this component exists) ─────────────────────────────────
  it('draws the same tile the store draws, from the agent id', () => {
    // The card this replaced hashed the first letter of the NAME into its own palette,
    // so one agent had two tiles either side of a tap. Pinning the id-keyed gradient
    // here is what keeps that from coming back.
    const fixture = create(view({ agentId: 'ast-42', emoji: '🐴' }));

    const tile: HTMLElement = fixture.nativeElement.querySelector('app-agent-icon span');
    expect(tile.style.backgroundImage).toBeTruthy();
    expect(gradientFor('ast-42')).not.toBe(gradientFor('Bronco Advisor'));
    expect(text(fixture)).toContain('🐴');
  });

  it('shows the tagline as the subtitle', () => {
    const fixture = create(
      view({ tagline: 'Degree requirements and deadlines.', description: 'Long description.' }),
    );
    expect(text(fixture)).toContain('Degree requirements and deadlines.');
    expect(text(fixture)).not.toContain('Long description.');
  });

  it('falls back to the description when no tagline was ever set', () => {
    // `tagline` is only written at listing submission — an unlisted agent has none.
    const fixture = create(view({ description: 'Long description.' }));
    expect(text(fixture)).toContain('Long description.');
  });

  it('attributes to the publisher, and to the owner when there is no publisher', () => {
    const published = create(
      view({
        publisher: { label: "Registrar's Office", kind: 'department', verified: true },
        ownerName: 'A. Author',
        categoryLabel: 'Advising',
      }),
    );
    // The spans are laid out with `gap-x`, so `textContent` runs them together — read the
    // attribution line as the parts it is actually built from.
    expect(attribution(published)).toEqual(['By', "Registrar's Office", '·', 'Advising']);
    expect(text(published)).not.toContain('A. Author');
    expect(published.nativeElement.querySelector('ng-icon[name="heroCheckBadge"]')).toBeTruthy();

    const plain = create(view({ ownerName: 'A. Author' }));
    expect(attribution(plain)).toEqual(['By', 'A. Author']);
    expect(plain.nativeElement.querySelector('ng-icon[name="heroCheckBadge"]')).toBeNull();
  });

  it('drops the "By" when there is only a category to show', () => {
    // A bare "By" with nothing after it reads as a rendering failure.
    const fixture = create(view({ categoryLabel: 'Advising' }));
    expect(attribution(fixture)).toEqual(['Advising']);
  });

  // ── starters are buttons here, unlike the detail page ───────────────────────────
  it('emits the starter it was given, so the composer can prefill it', () => {
    const fixture = create(view({ starters: ['When does registration close?'] }));
    let emitted: string | null = null;
    fixture.componentInstance.starterSelected.subscribe((s: string) => (emitted = s));

    const button: HTMLButtonElement = Array.from(
      fixture.nativeElement.querySelectorAll('button'),
    ).find((b) => (b as HTMLElement).textContent?.includes('registration')) as HTMLButtonElement;
    button.click();

    expect(emitted).toBe('When does registration close?');
  });

  // ── the detail link is gated on the listing; the pin control is not (#1897) ──────
  const pinButton = (fixture: { nativeElement: HTMLElement }) =>
    fixture.nativeElement.querySelector<HTMLButtonElement>('button[aria-pressed]');

  it('offers Add and a detail link for a listed agent', () => {
    const listed = create(view({ listed: true, pinnable: true }));
    expect(pinButton(listed)?.textContent?.trim()).toBe('Add');
    expect(text(listed)).toContain('Agent details');
    expect(listed.nativeElement.querySelector('a[href="/agents/ast-1"]')).toBeTruthy();
  });

  it('offers Pin, and no detail link, for an agent that is not in the store', () => {
    const unlisted = create(view({ listed: false, pinnable: true }));
    expect(pinButton(unlisted)?.textContent?.trim()).toBe('Pin');
    expect(text(unlisted)).not.toContain('Agent details');
    expect(unlisted.nativeElement.querySelector('a[href]')).toBeNull();
  });

  it('pins an unlisted agent and then says Pinned', async () => {
    const fixture = create(view({ listed: false, pinnable: true }));
    pinButton(fixture)!.click();
    http
      .expectOne((req) => req.method === 'POST' && req.url === '/api/agents/ast-1/pin')
      .flush({ agentId: 'ast-1', name: 'Bronco Advisor', locked: false });
    await fixture.whenStable();
    fixture.detectChanges();

    expect(pinButton(fixture)?.textContent?.trim()).toBe('Pinned');
    expect(pinButton(fixture)?.getAttribute('aria-pressed')).toBe('true');
  });

  it('shows a lock, not a toggle, for a locked role pin on an unlisted agent', async () => {
    const fixture = TestBed.createComponent(AgentLaunchCardComponent);
    fixture.componentRef.setInput('view', view({ listed: false, pinnable: true }));
    fixture.detectChanges();
    http
      .expectOne('/api/agents/pins')
      .flush({ pins: [{ agentId: 'ast-1', name: 'Bronco Advisor', locked: true }] });
    await fixture.whenStable();
    fixture.detectChanges();

    expect(pinButton(fixture)).toBeNull();
    expect(fixture.nativeElement.querySelector('ng-icon[name="heroLockClosed"]')).toBeTruthy();
    expect(text(fixture)).toContain('Pinned');
  });

  it('offers no pin control, and loads no pins, where pinning means nothing', () => {
    // The Designer's preview and the Assistant-shape fallback both pass `pinnable: false`.
    const fixture = TestBed.createComponent(AgentLaunchCardComponent);
    fixture.componentRef.setInput('view', view({ pinnable: false }));
    fixture.detectChanges();

    http.expectNone('/api/agents/pins');
    expect(pinButton(fixture)).toBeNull();
  });

  it('says Added for an agent already on the shelf', async () => {
    const fixture = TestBed.createComponent(AgentLaunchCardComponent);
    fixture.componentRef.setInput('view', view({ listed: true, pinnable: true }));
    fixture.detectChanges();
    http
      .expectOne('/api/agents/pins')
      .flush({ pins: [{ agentId: 'ast-1', name: 'Bronco Advisor', locked: false }] });
    // `load()` resolves through `firstValueFrom`, so the signal is written in a
    // microtask — flushing alone leaves the list still empty.
    await fixture.whenStable();
    fixture.detectChanges();

    expect(text(fixture)).toContain('Added');
  });

  // ── D6 ──────────────────────────────────────────────────────────────────────────
  it('stays silent when the agent runs fine', () => {
    // The card is read at the moment of typing; "this works" is not news there, and the
    // footer it used to force open pushed the starters up for nothing.
    const unknown = create(view({ listed: false }), null);
    expect(text(unknown)).not.toContain('Ready to run');
    expect(unknown.nativeElement.querySelector('.border-t')).toBeNull();

    const ready = create(view({ listed: false }), {
      agentId: 'ast-1',
      state: 'ready',
      missing: [],
    });
    expect(text(ready)).not.toContain('Ready to run');
    expect(ready.nativeElement.querySelector('.border-t')).toBeNull();
  });

  it('names what is missing when the agent is blocked', () => {
    const fixture = create(view(), {
      agentId: 'ast-1',
      state: 'blocked',
      missing: [{ label: 'Course Search', kind: 'tool', optional: false }],
    });
    expect(text(fixture)).toContain("“Course Search” isn't granted to your role.");
  });

  // ── detaching the agent from the conversation ───────────────────────────────────
  it('offers the close control only when the caller allows it', () => {
    const fixed = create(view());
    expect(fixed.nativeElement.querySelector('button[aria-label^="Leave"]')).toBeNull();

    const closable = TestBed.createComponent(AgentLaunchCardComponent);
    closable.componentRef.setInput('view', view());
    closable.componentRef.setInput('closable', true);
    closable.detectChanges();

    const button: HTMLButtonElement = closable.nativeElement.querySelector(
      'button[aria-label="Leave Bronco Advisor"]',
    );
    let closed = false;
    closable.componentInstance.closed.subscribe(() => (closed = true));
    button.click();
    expect(closed).toBe(true);
  });
});

describe('agentLaunchCardView', () => {
  const agent = (overrides: Partial<Agent> = {}): Agent =>
    ({
      agentId: 'ast-1',
      ownerName: 'A. Author',
      name: 'Bronco Advisor',
      description: 'Answers catalog questions.',
      bindings: [],
      visibility: 'PUBLIC',
      tags: [],
      starters: [],
      usageCount: 0,
      status: 'COMPLETE',
      createdAt: '2026-01-01T00:00:00Z',
      updatedAt: '2026-01-01T00:00:00Z',
      ...overrides,
    }) as Agent;

  it('treats only a published listing as listed', () => {
    expect(agentLaunchCardView(agent()).listed).toBe(false);
    expect(
      agentLaunchCardView(
        agent({ listing: { state: 'in_review', category: 'advising', publisherId: 'p1' } }),
      ).listed,
    ).toBe(false);
    expect(
      agentLaunchCardView(
        agent({ listing: { state: 'published', category: 'advising', publisherId: 'p1' } }),
      ).listed,
    ).toBe(true);
  });

  it('makes any saved agent pinnable, published or not', () => {
    // Pinning is gated on reach, not publication (#1897).
    expect(agentLaunchCardView(agent({ visibility: 'PRIVATE', status: 'DRAFT' })).pinnable).toBe(true);
    expect(
      agentLaunchCardView(
        agent({ listing: { state: 'published', category: 'advising', publisherId: 'p1' } }),
      ).pinnable,
    ).toBe(true);
  });

  it('never hands the card an undefined starter list', () => {
    // The list route omits `starters`; the card iterates it unguarded.
    expect(agentLaunchCardView(agent({ starters: undefined as unknown as string[] })).starters)
      .toEqual([]);
  });
});
