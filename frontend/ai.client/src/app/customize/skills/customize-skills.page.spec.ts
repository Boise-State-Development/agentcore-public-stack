import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { CustomizeSkillsPage } from './customize-skills.page';
import { SkillService, UserSkill } from '../../services/skill/skill.service';
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

const CATALOG: UserSkill[] = [
  skill({ skillId: 'sk_apa', displayName: 'APA Citations', isEnabled: true, userEnabled: true }),
  skill({ skillId: 'sk_syllabus', displayName: 'Syllabus Builder', category: 'teaching' }),
  skill({ skillId: 'sk_rubric', displayName: 'Rubric Writer', category: 'teaching' }),
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

  /** SkillService does NOT load in its constructor, so the page triggers it. */
  async function create(catalog: UserSkill[] = CATALOG) {
    const fixture = TestBed.createComponent(CustomizeSkillsPage);
    fixture.detectChanges();
    http.expectOne('/api/skills/').flush({ skills: catalog.map(s => ({ ...s })), totalCount: catalog.length });
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  const text = (fixture: { nativeElement: HTMLElement }) => fixture.nativeElement.textContent ?? '';

  it('loads the catalog itself, since SkillService defers its own load', async () => {
    const fixture = await create();
    expect(skills.initialized()).toBe(true);
    expect(fixture.nativeElement.querySelectorAll('app-customize-card')).toHaveLength(3);
  });

  it('counts what is on — skills default off, so most of the catalog is', async () => {
    const fixture = await create();
    expect(text(fixture)).toContain('1 of 3 skills on');
  });

  it('gives /my-skills the nav path it has never had', async () => {
    const fixture = await create();
    const link = fixture.nativeElement.querySelector('a[href="/my-skills"]');
    expect(link).toBeTruthy();
    expect(link.textContent).toContain('My skills');
  });

  it('searches across name, description and category', async () => {
    const fixture = await create();
    fixture.componentInstance['query'].set('rubric');
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelectorAll('app-customize-card')).toHaveLength(1);
    expect(text(fixture)).toContain('Rubric Writer');
  });

  it('filters by category chip', async () => {
    const fixture = await create();
    fixture.componentInstance['activeCategory'].set('teaching');
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelectorAll('app-customize-card')).toHaveLength(2);
  });

  it('writes a toggle through to the preferences endpoint', async () => {
    const fixture = await create();
    const toggles = fixture.nativeElement.querySelectorAll('app-customize-card button[role="switch"]');
    (toggles[1] as HTMLButtonElement).click();
    await fixture.whenStable();

    const req = http.expectOne('/api/skills/preferences');
    expect(req.request.body.preferences).toEqual({ sk_syllabus: true });
    req.flush({});
  });

  it('surfaces a failed save', async () => {
    const fixture = await create();
    const toggles = fixture.nativeElement.querySelectorAll('app-customize-card button[role="switch"]');
    (toggles[1] as HTMLButtonElement).click();
    await fixture.whenStable();

    http.expectOne('/api/skills/preferences').flush('nope', { status: 500, statusText: 'Error' });
    await settle();
    fixture.detectChanges();

    expect(text(fixture)).toContain("Couldn't save the change to Syllabus Builder");
    expect(skills.getSkill('sk_syllabus')?.isEnabled).toBe(false);
  });

  it('explains an empty catalog rather than showing a bare grid', async () => {
    const fixture = await create([]);
    expect(text(fixture)).toContain('No skills available');
  });

  describe('the agent-lock seam', () => {
    // See docs/specs/customize-surface.md §"The agent-lock seam", and the
    // matching block in customize-tools.page.spec.ts.

    it('shows the user their own catalog, not the Agent’s bound subset', async () => {
      skills.lockToAgentSkills(['sk_apa']);
      const fixture = await create();

      expect(skills.visibleSkills()).toHaveLength(1); // what the drawer would show
      expect(fixture.nativeElement.querySelectorAll('app-customize-card')).toHaveLength(3);
    });

    it('still saves a toggle while a lock is held', async () => {
      skills.lockToAgentSkills(['sk_apa']);
      const fixture = await create();

      const toggles = fixture.nativeElement.querySelectorAll(
        'app-customize-card button[role="switch"]',
      );
      (toggles[1] as HTMLButtonElement).click();
      await fixture.whenStable();

      const req = http.expectOne('/api/skills/preferences');
      expect(req.request.body.preferences).toEqual({ sk_syllabus: true });
      req.flush({});
    });
  });
});
