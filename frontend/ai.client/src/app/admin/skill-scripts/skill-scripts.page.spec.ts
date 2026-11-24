import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { ActivatedRoute, convertToParamMap, ParamMap, Router } from '@angular/router';
import { BehaviorSubject } from 'rxjs';
import { SkillScriptsPage } from './skill-scripts.page';
import { SkillScriptsHttpService } from './services/skill-scripts-http.service';
import { SkillScriptsDetail } from './models/skill-scripts.models';

const LIVE = '3f2a9c1e'.padEnd(64, '0');
const OLDER = 'a1b2c3d4'.padEnd(64, '1');

function detail(overrides: Partial<SkillScriptsDetail> = {}): SkillScriptsDetail {
  return {
    skillId: 'pdf_forms',
    displayName: 'PDF forms',
    status: 'active',
    ownerId: 'author-1',
    authorName: 'Dana Author',
    adminCatalog: false,
    provenance: { type: 'github', owner: 'acme', repo: 'skills' },
    scriptsHash: LIVE,
    currentSwitchedOff: false,
    scripts: [
      { path: 'scripts/fill.py', contentHash: 'c'.repeat(64), size: 22, source: "print('fill')", truncated: false, unreadable: false },
    ],
    versions: [
      {
        scriptsHash: LIVE,
        isCurrent: true,
        switchedOff: false,
        scripts: { 'scripts/fill.py': 'c'.repeat(64) },
        reviews: [
          {
            scopeType: 'AST', scopeId: 'agent-1', scopeKind: 'agent', scopeName: 'Grant Helper', scopeMissing: false,
            reviewedBy: 'reviewer-1', reviewerName: 'Riley Reviewer', reviewedAt: '2026-11-20T10:00:00+00:00',
            method: 'review', state: 'approved', version: 3,
          },
          {
            scopeType: 'AST', scopeId: 'harness-1', scopeKind: 'project', scopeName: 'Award Intake', scopeMissing: false,
            reviewedBy: 'author-1', reviewerName: 'Dana Author', reviewedAt: '2026-11-21T10:00:00+00:00',
            method: 'authored', state: 'approved', version: 3,
          },
        ],
      },
      { scriptsHash: OLDER, isCurrent: false, switchedOff: false, scripts: {}, reviews: [] },
    ],
    switch: { off: false, by: null, byUserId: null, byName: null, at: null, reason: null, hashes: [] },
    ...overrides,
  };
}

function setup(skillId: string | null, http: Partial<SkillScriptsHttpService>) {
  const params = new BehaviorSubject<ParamMap>(convertToParamMap(skillId ? { skillId } : {}));
  const route = {
    queryParamMap: params.asObservable(),
    get snapshot() {
      return { queryParamMap: params.value };
    },
  };
  const router = { navigate: vi.fn().mockResolvedValue(true) };
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: SkillScriptsHttpService, useValue: http },
      { provide: ActivatedRoute, useValue: route },
      { provide: Router, useValue: router },
    ],
  });
  const fixture = TestBed.createComponent(SkillScriptsPage);
  const el = fixture.nativeElement as HTMLElement;
  const settle = async () => {
    await fixture.whenStable();
    fixture.detectChanges();
  };
  return { fixture, el, router, params, settle };
}

function type(el: HTMLElement, selector: string, value: string) {
  const input = el.querySelector(selector) as HTMLInputElement | HTMLTextAreaElement;
  input.value = value;
  input.dispatchEvent(new Event('input'));
}

function button(el: HTMLElement, testId: string): HTMLButtonElement {
  return el.querySelector(`[data-testid="${testId}"]`) as HTMLButtonElement;
}

describe('SkillScriptsPage', () => {
  beforeEach(() => vi.clearAllMocks());

  it('loads the skill named in ?skillId= and shows its author, versions and source', async () => {
    const getDetail = vi.fn().mockResolvedValue(detail());
    const { el, settle } = setup('pdf_forms', { getDetail });
    await settle();

    expect(getDetail).toHaveBeenCalledWith('pdf_forms');
    expect(el.querySelector('h2')?.textContent).toContain('PDF forms');
    expect(el.querySelector('[data-testid="author"]')?.textContent).toContain('Dana Author');
    expect(el.querySelector('[data-testid="provenance"]')?.textContent).toContain('Imported from acme/skills');
    const live = el.querySelector('[data-testid="version-3f2a9c1e"]')!;
    expect(live.textContent).toContain('Live');
    expect(live.textContent).toContain('Agent: Grant Helper');
    expect(live.textContent).toContain('Project: Award Intake');
    expect(live.textContent).toContain('Bound by its author');
    expect(el.querySelector('[data-testid="version-a1b2c3d4"]')?.textContent).toContain('Not allowed in any Agent or project');
    expect(el.querySelector('pre code')?.textContent).toBe("print('fill')");
    expect(el.querySelector('[data-testid="switch-state"]')?.textContent).toContain('Scripts can run');
  });

  it('looks a skill up by putting its id in the URL', async () => {
    const { el, router, settle } = setup(null, { getDetail: vi.fn() });
    await settle();

    type(el, '#skill-scripts-id', '  other_skill ');
    (el.querySelector('form') as HTMLFormElement).dispatchEvent(new Event('submit'));

    expect(router.navigate).toHaveBeenCalledWith([], expect.objectContaining({ queryParams: { skillId: 'other_skill' } }));
  });

  it('says so when no skill has the id', async () => {
    const getDetail = vi.fn().mockRejectedValue(new HttpErrorResponse({ status: 404 }));
    const { el, settle } = setup('nope', { getDetail });
    await settle();

    expect(el.querySelector('[role="alert"]')?.textContent).toContain('No skill has that id.');
  });

  it('turns every version off only once a reason is given', async () => {
    const off = detail({
      currentSwitchedOff: true,
      switch: { off: true, by: 'admin', byUserId: 'admin-1', byName: 'Avery Admin', at: '2026-11-23T00:00:00+00:00', reason: 'Reported', hashes: [] },
    });
    const setSwitch = vi.fn().mockResolvedValue(off);
    const { el, fixture, settle } = setup('pdf_forms', { getDetail: vi.fn().mockResolvedValue(detail()), setSwitch });
    await settle();

    expect(button(el, 'turn-off').disabled).toBe(true);
    expect(button(el, 'turn-on')).toBeNull();
    type(el, '#skill-scripts-reason', 'Reported');
    fixture.detectChanges();
    button(el, 'turn-off').click();
    await settle();

    expect(setSwitch).toHaveBeenCalledWith('pdf_forms', { off: true, reason: 'Reported' });
    expect(el.querySelector('[data-testid="switch-state"]')?.textContent).toContain('An administrator');
    expect(el.querySelector('[data-testid="switch-state"]')?.textContent).toContain('every version');
    expect(button(el, 'turn-on')).not.toBeNull();
  });

  it('turns off only the ticked versions', async () => {
    const setSwitch = vi.fn().mockResolvedValue(detail());
    const { el, fixture, settle } = setup('pdf_forms', { getDetail: vi.fn().mockResolvedValue(detail()), setSwitch });
    await settle();

    const radios = el.querySelectorAll<HTMLInputElement>('input[name="switch-scope"]');
    radios[1].click();
    fixture.detectChanges();
    expect(button(el, 'turn-off').disabled).toBe(true);
    (el.querySelector('[aria-label="Select version a1b2c3d4"]') as HTMLInputElement).click();
    type(el, '#skill-scripts-reason', 'Old version is unsafe');
    fixture.detectChanges();
    button(el, 'turn-off').click();
    await settle();

    expect(setSwitch).toHaveBeenCalledWith('pdf_forms', { off: true, hashes: [OLDER], reason: 'Old version is unsafe' });
  });

  it('turns an author’s or admin’s switch back on', async () => {
    const off = detail({
      currentSwitchedOff: true,
      switch: { off: true, by: 'author', byUserId: 'author-1', byName: 'Dana Author', at: null, reason: null, hashes: [] },
    });
    const setSwitch = vi.fn().mockResolvedValue(detail());
    const { el, fixture, settle } = setup('pdf_forms', { getDetail: vi.fn().mockResolvedValue(off), setSwitch });
    await settle();

    expect(el.querySelector('[data-testid="switch-state"]')?.textContent).toContain('The author');
    type(el, '#skill-scripts-reason', 'Cleared after review');
    fixture.detectChanges();
    button(el, 'turn-on').click();
    await settle();

    expect(setSwitch).toHaveBeenCalledWith('pdf_forms', { off: false, reason: 'Cleared after review' });
    expect(el.querySelector('[data-testid="switch-state"]')?.textContent).toContain('Scripts can run');
  });

  it('shows the API’s refusal without dropping the detail', async () => {
    const setSwitch = vi.fn().mockRejectedValue(
      new HttpErrorResponse({ status: 422, error: { detail: 'Each hash must be a full 64-character script-set hash.' } }),
    );
    const { el, fixture, settle } = setup('pdf_forms', { getDetail: vi.fn().mockResolvedValue(detail()), setSwitch });
    await settle();

    type(el, '#skill-scripts-reason', 'x');
    fixture.detectChanges();
    button(el, 'turn-off').click();
    await settle();

    expect(el.querySelector('[role="alert"]')?.textContent).toContain('64-character');
    expect(el.querySelector('h2')?.textContent).toContain('PDF forms');
  });
});
