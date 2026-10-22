import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { CustomizeSkillsPage } from './customize-skills.page';
import { SkillService, UserSkill } from '../../services/skill/skill.service';
import { MySkill } from './models/my-skill.model';
import { ConfigService } from '../../services/config.service';

/**
 * The rejection from a failed save travels catch → signal set, which lands a
 * microtask after `whenStable()` has already resolved on the HTTP task. One
 * macrotask tick is what makes the banner observable.
 */
const settle = () => new Promise(resolve => setTimeout(resolve, 0));

const skill = (
  overrides: Partial<UserSkill> & Pick<UserSkill, 'skillId' | 'displayName'>,
): UserSkill => ({
  description: 'Knows a thing.',
  category: 'writing',
  userEnabled: null,
  isEnabled: false,
  ...overrides,
});

const mine = (
  overrides: Partial<MySkill> & Pick<MySkill, 'skillId' | 'displayName'>,
): MySkill => ({
  description: 'Something I wrote.',
  instructions: '',
  allowedTools: [],
  skillMetadata: {},
  resources: [],
  status: 'active',
  category: null,
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-02-01T00:00:00Z',
  ...overrides,
});

/**
 * `sk_own` is BOTH authored and in the picker feed — that is the real shape:
 * `resolve_accessible_skill_ids` unions the catalog with what you own, so an
 * ACTIVE authored skill appears in `GET /skills/` too. The page must not double
 *it into Discover.
 */
const CATALOG: UserSkill[] = [
  skill({ skillId: 'sk_apa', displayName: 'APA Citations', isEnabled: true, userEnabled: true }),
  skill({ skillId: 'sk_syllabus', displayName: 'Syllabus Builder', category: 'teaching' }),
  skill({ skillId: 'sk_rubric', displayName: 'Rubric Writer', category: 'teaching' }),
  skill({ skillId: 'sk_own', displayName: 'My Own Skill' }),
];

const MINE: MySkill[] = [
  mine({ skillId: 'sk_own', displayName: 'My Own Skill' }),
  // Never in `GET /skills/`: a draft is filtered out by status. Its author must
  // still see it, which is the whole reason this page reads two endpoints.
  mine({ skillId: 'sk_draft', displayName: 'Half Finished', status: 'draft' }),
];

describe('CustomizeSkillsPage', () => {
  let http: HttpTestingController;
  let skills: SkillService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])],
    });
    TestBed.inject(ConfigService).appApiUrl.set('/api');
    http = TestBed.inject(HttpTestingController);
    skills = TestBed.inject(SkillService);
  });

  afterEach(() => {
    http.verify();
    TestBed.resetTestingModule();
  });

  /**
   * SkillService does NOT load in its constructor, so the page triggers it —
   * and it triggers the authored read alongside it. Both must be flushed or
   * `http.verify()` fails the test.
   */
  async function create(
    catalog: UserSkill[] = CATALOG,
    authored: MySkill[] | 'unavailable' = MINE,
  ) {
    const fixture = TestBed.createComponent(CustomizeSkillsPage);
    fixture.detectChanges();

    http
      .expectOne('/api/skills/')
      .flush({ skills: catalog.map(s => ({ ...s })), totalCount: catalog.length });

    const authoredReq = http.expectOne('/api/skills/mine');
    if (authored === 'unavailable') {
      authoredReq.flush('off', { status: 404, statusText: 'Not Found' });
    } else {
      authoredReq.flush({ skills: authored.map(s => ({ ...s })), totalCount: authored.length });
    }

    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  const text = (fixture: { nativeElement: HTMLElement }) => fixture.nativeElement.textContent ?? '';
  const rows = (fixture: { nativeElement: HTMLElement }) =>
    [...fixture.nativeElement.querySelectorAll('app-skill-row')];
  const cards = (fixture: { nativeElement: HTMLElement }) =>
    [...fixture.nativeElement.querySelectorAll('app-customize-card')];

  /**
   * In the app the scope arrives as the `scope` query param, bound by
   * `withComponentInputBinding()`. `setScope` only writes that param, and the
   * test router has no matched route to bind it back through — so drive the
   * input directly, exactly as the router would.
   */
  function toDiscover(fixture: ComponentFixture<CustomizeSkillsPage>): void {
    fixture.componentRef.setInput('scope', 'discover');
    fixture.detectChanges();
  }

  it('reads both tiers — the picker feed and the authored tier', async () => {
    const fixture = await create();
    expect(skills.initialized()).toBe(true);
    expect(text(fixture)).toContain('Created by you');
  });

  describe('Yours', () => {
    it('opens on Yours, not Discover', async () => {
      const fixture = await create();
      expect(fixture.componentInstance['scope']()).toBe('yours');
      expect(cards(fixture)).toHaveLength(0);
    });

    it('groups authored skills apart from the catalog ones you switched on', async () => {
      const fixture = await create();
      expect(text(fixture)).toContain('Created by you');
      expect(text(fixture)).toContain('From the catalog');
      // Authored: sk_own + sk_draft. Catalog-and-on: sk_apa only.
      expect(rows(fixture)).toHaveLength(3);
      expect(text(fixture)).toContain('My Own Skill');
      expect(text(fixture)).toContain('Half Finished');
      expect(text(fixture)).toContain('APA Citations');
    });

    it('keeps an authored skill out of the catalog group even though both feeds carry it', async () => {
      const fixture = await create();
      const catalogSection = [...fixture.nativeElement.querySelectorAll('section')].find(s =>
        (s.textContent ?? '').includes('From the catalog'),
      );
      expect(catalogSection?.textContent).not.toContain('My Own Skill');
    });

    it('shows a draft you authored, which GET /skills/ never returns', async () => {
      const fixture = await create();
      expect(text(fixture)).toContain('Half Finished');
      expect(text(fixture)).toContain('draft');
    });

    it('offers no Turn on for a draft — there is nothing in the feed to toggle', async () => {
      const fixture = await create();
      const draftRow = rows(fixture).find(r => (r.textContent ?? '').includes('Half Finished'));
      expect(draftRow?.textContent).not.toContain('Turn on');

      const ownRow = rows(fixture).find(r => (r.textContent ?? '').includes('My Own Skill'));
      expect(ownRow?.textContent).toContain('Turn on');
    });

    it('links each row to its detail page', async () => {
      const fixture = await create();
      const hrefs = rows(fixture)
        .map(r => r.querySelector('h3 a')?.getAttribute('href'))
        .filter(Boolean);
      expect(hrefs).toContain('/customize/skills/sk_own');
      expect(hrefs).toContain('/customize/skills/sk_draft');
      expect(hrefs).toContain('/customize/skills/sk_apa');
    });

    it('writes a toggle through to the preferences endpoint', async () => {
      const fixture = await create();
      const ownRow = rows(fixture).find(r => (r.textContent ?? '').includes('My Own Skill'));
      const turnOn = [...(ownRow?.querySelectorAll('button') ?? [])].find(b =>
        (b.textContent ?? '').includes('Turn on'),
      );
      (turnOn as HTMLButtonElement).click();
      await fixture.whenStable();

      const req = http.expectOne('/api/skills/preferences');
      expect(req.request.body.preferences).toEqual({ sk_own: true });
      req.flush({});
    });

    it('surfaces a failed save', async () => {
      const fixture = await create();
      const ownRow = rows(fixture).find(r => (r.textContent ?? '').includes('My Own Skill'));
      const turnOn = [...(ownRow?.querySelectorAll('button') ?? [])].find(b =>
        (b.textContent ?? '').includes('Turn on'),
      );
      (turnOn as HTMLButtonElement).click();
      await fixture.whenStable();

      http.expectOne('/api/skills/preferences').flush('nope', { status: 500, statusText: 'Error' });
      await settle();
      fixture.detectChanges();

      expect(text(fixture)).toContain("Couldn't save the change to My Own Skill");
      expect(skills.getSkill('sk_own')?.isEnabled).toBe(false);
    });

    it('explains an empty page rather than showing bare headings', async () => {
      const fixture = await create([], []);
      expect(text(fixture)).toContain('Nothing here yet');
    });
  });

  describe('Discover', () => {
    it('shows only catalog skills that are still off', async () => {
      const fixture = await create();
      toDiscover(fixture);

      // sk_syllabus + sk_rubric. sk_apa is on; sk_own is authored; sk_draft
      // is not in the feed at all.
      expect(cards(fixture)).toHaveLength(2);
      expect(text(fixture)).toContain('Syllabus Builder');
      expect(text(fixture)).toContain('Rubric Writer');
      expect(text(fixture)).not.toContain('APA Citations');
    });

    it('counts what is available to turn on', async () => {
      const fixture = await create();
      toDiscover(fixture);
      expect(text(fixture)).toContain('2 skills available to turn on');
    });

    it('links each card to its detail page', async () => {
      const fixture = await create();
      toDiscover(fixture);
      const links = cards(fixture).map(c => c.querySelector('a')?.getAttribute('href'));
      expect(links).toEqual(['/customize/skills/sk_syllabus', '/customize/skills/sk_rubric']);
    });

    it('keeps the switch out of the card link', async () => {
      // A control nested inside a link is a control the user cannot operate
      // with the keyboard without also following the link.
      const fixture = await create();
      toDiscover(fixture);
      const toggle = fixture.nativeElement.querySelector(
        'app-customize-card button[role="switch"]',
      );
      expect(toggle.closest('a')).toBeNull();
    });

    it('filters by category chip', async () => {
      const fixture = await create();
      toDiscover(fixture);
      fixture.componentInstance['activeCategory'].set('teaching');
      fixture.detectChanges();
      expect(cards(fixture)).toHaveLength(2);
    });

    it('says so when there is nothing left to turn on', async () => {
      const fixture = await create(
        [skill({ skillId: 'sk_apa', displayName: 'APA Citations', isEnabled: true })],
        [],
      );
      toDiscover(fixture);
      expect(text(fixture)).toContain("You've turned on everything");
    });
  });

  describe('search', () => {
    it('searches the authored tier', async () => {
      const fixture = await create();
      fixture.componentInstance['query'].set('half');
      fixture.detectChanges();
      expect(rows(fixture)).toHaveLength(1);
      expect(text(fixture)).toContain('Half Finished');
    });

    it('searches the catalog tier in Discover', async () => {
      const fixture = await create();
      toDiscover(fixture);
      fixture.componentInstance['query'].set('rubric');
      fixture.detectChanges();
      expect(cards(fixture)).toHaveLength(1);
      expect(text(fixture)).toContain('Rubric Writer');
    });
  });

  describe('authoring affordances', () => {
    it('offers the Add menu when the authored tier is reachable', async () => {
      const fixture = await create();
      const add = [...fixture.nativeElement.querySelectorAll('button')].find(b =>
        (b.textContent ?? '').trim().startsWith('Add'),
      );
      expect(add).toBeTruthy();
    });

    it('hides Add when SKILLS_ENABLED is off — the form could only fail to save', async () => {
      const fixture = await create(CATALOG, 'unavailable');
      const add = [...fixture.nativeElement.querySelectorAll('button')].find(b =>
        (b.textContent ?? '').trim().startsWith('Add'),
      );
      expect(add).toBeUndefined();
    });

    it('does not link out to /my-skills — this page absorbed it', async () => {
      const fixture = await create();
      expect(fixture.nativeElement.querySelector('a[href^="/my-skills"]')).toBeNull();
    });
  });

  describe('the agent-lock seam', () => {
    // See docs/specs/customize-surface.md §"The agent-lock seam", and the
    // matching block in customize-tools.page.spec.ts.

    it('shows the user their own catalog, not the Agent’s bound subset', async () => {
      skills.lockToAgentSkills(['sk_apa']);
      const fixture = await create();
      toDiscover(fixture);

      expect(skills.visibleSkills()).toHaveLength(1); // what the drawer would show
      expect(cards(fixture)).toHaveLength(2);
    });

    it('still saves a toggle while a lock is held', async () => {
      skills.lockToAgentSkills(['sk_apa']);
      const fixture = await create();
      toDiscover(fixture);

      const toggles = fixture.nativeElement.querySelectorAll(
        'app-customize-card button[role="switch"]',
      );
      (toggles[0] as HTMLButtonElement).click();
      await fixture.whenStable();

      const req = http.expectOne('/api/skills/preferences');
      expect(req.request.body.preferences).toEqual({ sk_syllabus: true });
      req.flush({});
    });
  });
});
