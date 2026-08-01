import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { AgentSpotlightComponent } from './agent-spotlight.component';
import { gradientFor } from './agent-icon.component';
import { AgentPinService } from '../services/agent-pin.service';
import { AgentListing, PinnedAgent } from '../models/store.model';
import { ConfigService } from '../../services/config.service';

describe('AgentSpotlightComponent', () => {
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
    agentId: 'ast-42',
    name: 'Bronco Advisor',
    tagline: 'Degree requirements and deadlines.',
    category: 'advising',
    ...overrides,
  });

  async function withPins(pins: Partial<PinnedAgent>[]) {
    const service = TestBed.inject(AgentPinService);
    const load = service.load();
    http.expectOne('/api/agents/pins').flush({ pins });
    await load;
  }

  function create(input: AgentListing | null) {
    const fixture = TestBed.createComponent(AgentSpotlightComponent);
    fixture.componentRef.setInput('listing', input);
    fixture.detectChanges();
    return fixture;
  }

  const text = (fixture: { nativeElement: HTMLElement }) => fixture.nativeElement.textContent ?? '';

  it('renders nothing when an admin has curated no featured agent', () => {
    expect(text(create(null)).trim()).toBe('');
  });

  it('is tinted by the featured agent’s own tile gradient', () => {
    // Same `gradientFor(agentId)` every other surface draws from, so the front door can
    // never clash with the artwork sitting in it.
    const background = create(listing()).componentInstance.background();
    expect(background).toContain(gradientFor('ast-42'));
  });

  it('always lays a scrim over that gradient', () => {
    // Load-bearing, not decoration: two of the twelve palette entries (amber→orange,
    // lime→green) put white body text under 4.5:1 on their own. The scrim makes the
    // contrast a property of the component rather than of which agent got featured, so
    // no future palette entry can silently break this band.
    for (const agentId of ['ast-1', 'ast-2', 'ast-3', 'ast-4', 'ast-5', 'ast-6']) {
      const background = create(listing({ agentId })).componentInstance.background();
      expect(background.startsWith('linear-gradient(rgba(2, 6, 23, 0.6)')).toBe(true);
    }
  });

  it('names the agent, its tagline and its publisher', () => {
    const fixture = create(
      listing({ publisher: { label: "Registrar's Office", kind: 'department', verified: true } }),
    );
    expect(text(fixture)).toContain('Featured');
    expect(text(fixture)).toContain('Bronco Advisor');
    expect(text(fixture)).toContain('Degree requirements and deadlines.');
    expect(text(fixture)).toContain("Registrar's Office");
    expect(fixture.nativeElement.querySelector('ng-icon[name="heroCheckBadge"]')).toBeTruthy();
  });

  it('offers Start chat and Add, and reflects an existing pin', async () => {
    const fresh = create(listing());
    expect(text(fresh)).toContain('Start chat');
    expect(text(fresh)).toContain('Add');

    await withPins([{ agentId: 'ast-42', name: 'Bronco Advisor', locked: false }]);
    const pinned = create(listing());
    expect(text(pinned)).toContain('Added');
  });

  it('says who decided when the pin is locked to a role', async () => {
    await withPins([{ agentId: 'ast-42', name: 'Bronco Advisor', locked: true }]);
    const fixture = create(listing());
    expect(text(fixture)).toContain('Added by your role');
  });

  it('links the name to the detail page', () => {
    const fixture = create(listing());
    expect(fixture.nativeElement.querySelector('a[href="/agents/ast-42"]')).toBeTruthy();
  });
});
