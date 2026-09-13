import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { provideMarkdown } from 'ngx-markdown';
import { CustomizeSkillDetailPage } from './customize-skill-detail.page';
import { SkillService, SkillsResponse } from '../../services/skill/skill.service';
import { SkillDetail } from '../../services/skill-detail/skill-detail.service';
import { ConfigService } from '../../services/config.service';

/** A failed save lands a microtask after `whenStable()`; one macrotask makes it observable. */
const settle = () => new Promise(resolve => setTimeout(resolve, 0));

const detail = (
  overrides: Partial<SkillDetail> & Pick<SkillDetail, 'skillId' | 'displayName'>,
): SkillDetail => ({
  description: 'Does a thing.',
  instructions: '',
  compose: [],
  allowedTools: [],
  skillMetadata: {},
  resources: [],
  status: 'active',
  category: null,
  userEnabled: null,
  isEnabled: false,
  isOwned: false,
  createdAt: null,
  updatedAt: null,
  ...overrides,
});

const PICKER: SkillsResponse = {
  skills: [
    {
      skillId: 'web_research',
      displayName: 'Web Research',
      description: 'Does a thing.',
      category: 'research',
      userEnabled: null,
      isEnabled: false,
    },
  ],
  totalCount: 1,
};

describe('CustomizeSkillDetailPage', () => {
  let http: HttpTestingController;
  let skills: SkillService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        provideMarkdown(),
      ],
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
   * Creates the page and flushes both reads it makes: the picker list (warmed
   * so the toggle has a row to write through) and the detail record.
   */
  async function create(
    skillId: string,
    record: SkillDetail | null,
    status = 404,
  ): Promise<ComponentFixture<CustomizeSkillDetailPage>> {
    const fixture = TestBed.createComponent(CustomizeSkillDetailPage);
    fixture.componentRef.setInput('skillId', skillId);
    fixture.detectChanges();

    http.match(req => req.url === '/api/skills/').forEach(req => req.flush(PICKER));

    const url = `/api/skills/${encodeURIComponent(skillId)}`;
    http.match(url).forEach(req => {
      if (record) req.flush(record);
      else req.flush({ detail: 'nope' }, { status, statusText: 'Not Found' });
    });

    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  const text = (fixture: ComponentFixture<CustomizeSkillDetailPage>) =>
    (fixture.nativeElement as HTMLElement).textContent ?? '';

  it('shows the skill it was routed to', async () => {
    const fixture = await create(
      'web_research',
      detail({ skillId: 'web_research', displayName: 'Web Research' }),
    );
    expect(text(fixture)).toContain('Web Research');
    expect(text(fixture)).toContain('web_research');
  });

  it('renders the SKILL.md body expanded, with no click needed', async () => {
    const fixture = await create(
      'web_research',
      detail({
        skillId: 'web_research',
        displayName: 'Web Research',
        instructions: '# How to research\n\nSearch broadly, cite everything.',
      }),
    );
    await fixture.whenStable();
    fixture.detectChanges();

    expect(text(fixture)).toContain('Search broadly, cite everything.');
    // The heading came through the markdown renderer, not as literal `#`.
    expect(fixture.nativeElement.querySelector('markdown h1')?.textContent).toContain(
      'How to research',
    );
  });

  it('says so when a skill carries no instructions body', async () => {
    const fixture = await create(
      'thin_one',
      detail({ skillId: 'thin_one', displayName: 'Thin One', instructions: '' }),
    );
    expect(text(fixture)).toContain('no instructions body');
  });

  it('lists supporting files with an access-scoped link, not the owner route', async () => {
    const fixture = await create(
      'web_research',
      detail({
        skillId: 'web_research',
        displayName: 'Web Research',
        resources: [
          {
            filename: 'forms.md',
            contentHash: 'abc',
            size: 2048,
            contentType: 'text/markdown',
            s3Key: 'skills/web_research/references/forms.md',
            kind: 'reference',
          },
        ],
      }),
    );

    expect(text(fixture)).toContain('forms.md');
    expect(text(fixture)).toContain('2 KB');
    const link = fixture.nativeElement.querySelector('a[href*="forms.md"]') as HTMLAnchorElement;
    // `/skills/{id}/resources/...`, never `/skills/mine/...` — the owner route
    // 404s for a catalog skill, which is most of what this page shows.
    expect(link.getAttribute('href')).toBe('/api/skills/web_research/resources/forms.md');
    expect(link.getAttribute('href')).not.toContain('/mine/');
  });

  it('marks frontmatter tool names as advisory, never as a grant', async () => {
    const fixture = await create(
      'web_research',
      detail({
        skillId: 'web_research',
        displayName: 'Web Research',
        allowedTools: ['web_search'],
      }),
    );
    expect(text(fixture)).toContain('web_search');
    // Skills v2 D4: naming a tool never grants it. A bare list would read as one.
    expect(text(fixture)).toContain('not a grant');
  });

  it('offers an edit path only for a skill the user wrote', async () => {
    const mine = await create(
      'my_skill',
      detail({ skillId: 'my_skill', displayName: 'My Skill', isOwned: true }),
    );
    expect(text(mine)).toContain('Edit in My Skills');
    // The route is /my-skills/:skillId/edit — a bare /my-skills/:id does not exist.
    expect(
      mine.nativeElement.querySelector('a[href="/my-skills/my_skill/edit"]'),
    ).toBeTruthy();
  });

  it('offers no edit path for a catalog skill', async () => {
    const fixture = await create(
      'web_research',
      detail({ skillId: 'web_research', displayName: 'Web Research', isOwned: false }),
    );
    expect(text(fixture)).not.toContain('Edit in My Skills');
  });

  it('distinguishes a missing skill from a failed read', async () => {
    const missing = await create('gone', null, 404);
    expect(text(missing)).toContain('Skill not found');
  });

  it('does not claim a skill is gone when the read merely failed', async () => {
    const fixture = await create('web_research', null, 500);
    expect(text(fixture)).toContain("Couldn't load this skill");
    // A 500 says nothing about whether the skill exists; saying it is gone
    // would be a lie the user cannot check.
    expect(text(fixture)).not.toContain('Skill not found');
  });

  it('writes the toggle globally, never through the agent lock', async () => {
    skills.lockToAgentSkills([]);
    const fixture = await create(
      'web_research',
      detail({ skillId: 'web_research', displayName: 'Web Research' }),
    );

    (fixture.nativeElement.querySelector('button[role="switch"]') as HTMLButtonElement).click();
    await settle();

    // An agent-locked page must still save: this surface is global state, and
    // `respectAgentLock: false` is what makes that true (spec §agent-lock seam).
    http.expectOne('/api/skills/preferences').flush({});
    expect(skills.skills()[0].isEnabled).toBe(true);
  });

  it('says so when a save fails instead of letting the switch snap back', async () => {
    const fixture = await create(
      'web_research',
      detail({ skillId: 'web_research', displayName: 'Web Research' }),
    );

    (fixture.nativeElement.querySelector('button[role="switch"]') as HTMLButtonElement).click();
    http
      .expectOne('/api/skills/preferences')
      .flush({ detail: 'boom' }, { status: 500, statusText: 'Server Error' });
    await settle();
    fixture.detectChanges();

    expect(text(fixture)).toContain("Couldn't save the change to Web Research");
    expect(skills.skills()[0].isEnabled).toBe(false);
  });

  it('keeps the switch inert until the picker list has landed', async () => {
    // `SkillService.toggleSkill` silently no-ops on a skill it never loaded, so
    // a deep link that clicked before the list arrived would look like a broken
    // switch. The guard makes the dead moment visible instead.
    const fixture = TestBed.createComponent(CustomizeSkillDetailPage);
    fixture.componentRef.setInput('skillId', 'web_research');
    fixture.detectChanges();

    http
      .match('/api/skills/web_research')
      .forEach(req =>
        req.flush(detail({ skillId: 'web_research', displayName: 'Web Research' })),
      );
    await fixture.whenStable();
    fixture.detectChanges();

    const toggle = fixture.nativeElement.querySelector(
      'button[role="switch"]',
    ) as HTMLButtonElement;
    expect(toggle.disabled).toBe(true);

    http.match(req => req.url === '/api/skills/').forEach(req => req.flush(PICKER));
    await fixture.whenStable();
    fixture.detectChanges();
    expect(
      (fixture.nativeElement.querySelector('button[role="switch"]') as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });
});
