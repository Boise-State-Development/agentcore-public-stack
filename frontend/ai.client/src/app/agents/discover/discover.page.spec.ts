import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { AgentDiscoverPage } from './discover.page';
import { AgentStoreService } from '../services/agent-store.service';
import { AgentListing, CategoryShelf } from '../models/store.model';
import { ConfigService } from '../../services/config.service';

const listing = (agentId: string, name: string, category: string): AgentListing => ({
  agentId,
  name,
  tagline: `${name} does a thing.`,
  category,
});

const shelf = (id: string, label: string, listings: AgentListing[]): CategoryShelf => ({
  category: { id, label, order: 0, enabled: true },
  listings,
});

const FEATURED = [
  listing('ast-1', 'Bronco Advisor', 'advising'),
  listing('ast-9', 'Grant Finder', 'research'),
];

const SHELVES = [
  shelf('advising', 'Advising', [
    listing('ast-1', 'Bronco Advisor', 'advising'),
    listing('ast-2', 'Registration Coach', 'advising'),
  ]),
  shelf('research', 'Research', [listing('ast-3', 'Lab Notebook', 'research')]),
];

/** Stands in for the real store service; the page only calls `loadDiscover`. */
class FakeStoreService {
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  featured: AgentListing[] = FEATURED;
  shelves: CategoryShelf[] = SHELVES;

  async loadDiscover() {
    return { featured: this.featured, shelves: this.shelves };
  }
}

describe('AgentDiscoverPage', () => {
  let http: HttpTestingController;
  let store: FakeStoreService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    store = new FakeStoreService();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: AgentStoreService, useValue: store },
      ],
    });
    TestBed.inject(ConfigService).appApiUrl.set('/api');
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => TestBed.resetTestingModule());

  async function create() {
    const fixture = TestBed.createComponent(AgentDiscoverPage);
    fixture.detectChanges();
    // The page loads pins independently of the shelves.
    http.match('/api/agents/pins').forEach((req) => req.flush({ pins: [] }));
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  const text = (fixture: { nativeElement: HTMLElement }) => fixture.nativeElement.textContent ?? '';

  it('gives the front door to the first featured agent', async () => {
    const fixture = await create();
    expect(fixture.componentInstance.spotlight()?.agentId).toBe('ast-1');
    expect(fixture.nativeElement.querySelector('app-agent-spotlight')).toBeTruthy();
  });

  it('keeps the rest of Featured as an ordinary shelf', async () => {
    // Not folded into the category shelves and forgotten: those page at 12, so an older
    // featured agent can fall off the shelf it belongs to.
    const fixture = await create();
    expect(fixture.componentInstance.alsoFeatured().map((l) => l.agentId)).toEqual(['ast-9']);
    expect(text(fixture)).toContain('Also featured');
  });

  it('renders no spotlight when nothing is curated', async () => {
    store.featured = [];
    const fixture = await create();
    expect(fixture.componentInstance.spotlight()).toBeNull();
    expect(fixture.nativeElement.querySelector('app-agent-spotlight')).toBeNull();
  });

  it('offers a chip per non-empty shelf, plus All', async () => {
    const fixture = await create();
    expect(fixture.componentInstance.categoryChips().map((c) => c.label)).toEqual([
      'All',
      'Advising',
      'Research',
    ]);
  });

  it('filters the shelves to the chosen category', async () => {
    const fixture = await create();
    fixture.componentInstance.onCategory('research');
    fixture.detectChanges();

    expect(fixture.componentInstance.visibleShelves().map((s) => s.category.id)).toEqual([
      'research',
    ]);
    // Merchandising belongs to the unfiltered view — the user has asked a narrower question.
    expect(fixture.componentInstance.isAllCategories()).toBe(false);
    expect(fixture.nativeElement.querySelector('app-agent-spotlight')).toBeNull();
  });

  it('clears the filter when the active chip is tapped again', async () => {
    const fixture = await create();
    fixture.componentInstance.onCategory('research');
    fixture.componentInstance.onCategory('research');
    fixture.detectChanges();

    expect(fixture.componentInstance.isAllCategories()).toBe(true);
    expect(fixture.componentInstance.visibleShelves().length).toBe(2);
  });

  it('collapses to a flat result list while searching', async () => {
    const fixture = await create();
    fixture.componentInstance.query.set('registration');
    fixture.detectChanges();

    expect(fixture.componentInstance.searchResults().map((l) => l.agentId)).toEqual(['ast-2']);
    expect(fixture.nativeElement.querySelector('app-agent-spotlight')).toBeNull();
    expect(text(fixture)).toContain('1 result');
  });

  it('renders shelves as tiles rather than rows', async () => {
    const fixture = await create();
    expect(fixture.nativeElement.querySelectorAll('app-agent-store-tile').length).toBeGreaterThan(0);
    expect(fixture.nativeElement.querySelector('app-agent-listing-row')).toBeNull();
  });
});
