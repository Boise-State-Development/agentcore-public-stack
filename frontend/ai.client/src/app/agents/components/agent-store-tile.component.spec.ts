import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { AgentStoreTileComponent } from './agent-store-tile.component';
import { AgentPinService } from '../services/agent-pin.service';
import { AgentListing, PinnedAgent } from '../models/store.model';
import { ConfigService } from '../../services/config.service';

describe('AgentStoreTileComponent', () => {
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

  const listing = (overrides: Partial<AgentListing> = {}): AgentListing => ({
    agentId: 'ast-1',
    name: 'Bronco Advisor',
    tagline: 'Degree requirements and deadlines.',
    category: 'advising',
    ...overrides,
  });

  /** Seeds the session-wide pin list before the tile reads it. */
  async function withPins(pins: Partial<PinnedAgent>[]) {
    const service = TestBed.inject(AgentPinService);
    const load = service.load();
    http.expectOne('/api/agents/pins').flush({ pins });
    await load;
  }

  function create(input: AgentListing) {
    const fixture = TestBed.createComponent(AgentStoreTileComponent);
    fixture.componentRef.setInput('listing', input);
    fixture.detectChanges();
    return fixture;
  }

  const text = (fixture: { nativeElement: HTMLElement }) => fixture.nativeElement.textContent ?? '';

  it('leads with the tagline, falling back to the publisher', () => {
    expect(text(create(listing()))).toContain('Degree requirements and deadlines.');

    const noTagline = create(
      listing({
        tagline: undefined,
        publisher: { label: "Registrar's Office", kind: 'department', verified: false },
      }),
    );
    expect(text(noTagline)).toContain("Registrar's Office");
  });

  it('draws the artwork at store size', () => {
    // 52 rather than the row's 40: the icon is what a person recognises a week later.
    const fixture = create(listing());
    const tile: HTMLElement = fixture.nativeElement.querySelector('app-agent-icon span');
    expect(tile.className).toContain('size-13');
  });

  it('shows the add control without waiting for a hover', () => {
    // The shelf row it replaced was `opacity-0 group-hover:opacity-100`, which on a
    // touch device means the store's primary verb does not exist. Pin this.
    const fixture = create(listing());
    const button: HTMLButtonElement = fixture.nativeElement.querySelector('button');

    expect(button).toBeTruthy();
    expect(button.className).not.toContain('opacity-0');
    expect(button.getAttribute('aria-pressed')).toBe('false');
  });

  it('becomes a check once the agent is pinned', async () => {
    await withPins([{ agentId: 'ast-1', name: 'Bronco Advisor', locked: false }]);
    const fixture = create(listing());

    const button: HTMLButtonElement = fixture.nativeElement.querySelector('button');
    expect(button.getAttribute('aria-pressed')).toBe('true');
    expect(text(fixture)).toContain('Remove Bronco Advisor from your agents');
  });

  it('replaces the control with a lock for a role-locked pin', async () => {
    // D9.4 — absent, not disabled. A disabled toggle invites a click and then refuses it.
    await withPins([{ agentId: 'ast-1', name: 'Bronco Advisor', locked: true }]);
    const fixture = create(listing());

    expect(fixture.nativeElement.querySelector('button')).toBeNull();
    expect(text(fixture)).toContain("can't be removed");
  });

  it('links the body to the detail page', () => {
    const fixture = create(listing());
    expect(fixture.nativeElement.querySelector('a[href="/agents/ast-1"]')).toBeTruthy();
  });

  it('carries no dependency counts (D4)', () => {
    const fixture = create(listing());
    const body = text(fixture);
    expect(body).not.toMatch(/tool|skill|memory|model/i);
  });
});
