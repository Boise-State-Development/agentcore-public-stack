import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { ConfigService } from '../../../services/config.service';
import { SkillScriptStatus } from '../script-review.model';
import { SkillScriptsBadgeComponent, scriptsTrustLine } from './skill-scripts-badge.component';
import { SkillScriptsSwitchComponent } from './skill-scripts-switch.component';

const status = (over: Partial<SkillScriptStatus> = {}): SkillScriptStatus => ({
  skillId: 'pdf',
  displayName: 'PDF',
  version: 3,
  scriptsHash: '3f2a9c1e' + '0'.repeat(56),
  state: 'allowed',
  method: 'review',
  author: 'Pat',
  reviewer: 'Sam',
  reviewedAt: '2026-11-23T10:00:00Z',
  provenance: { type: 'zip' },
  ...over,
});

const text = (el: HTMLElement) => (el.textContent ?? '').replace(/\s+/g, ' ').trim();

describe('SkillScriptsBadgeComponent (skill-code-sandbox §8.6)', () => {
  function render(skills: SkillScriptStatus[]) {
    const fixture = TestBed.createComponent(SkillScriptsBadgeComponent);
    fixture.componentRef.setInput('skills', skills);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('one skill: says scripts run with no internet access, and who reviewed them', () => {
    expect(text(render([status()]))).toBe('Runs scripts in a sandbox with no internet access · Reviewed by Sam, Nov 23, 2026');
  });

  it("a hand-written skill reads 'Shared by' its author, a catalog skill 'From the admin catalog'", () => {
    expect(scriptsTrustLine(status({ state: 'authored', method: 'authored' }))).toBe('Shared by Pat, Nov 23, 2026');
    expect(scriptsTrustLine(status({ state: 'admin', reviewedAt: null }))).toBe('From the admin catalog');
  });

  it('several skills: a count, with each one listed', () => {
    const el = render([status(), status({ skillId: 'docx', displayName: 'DOCX', state: 'authored', method: 'authored' })]);
    expect(text(el)).toContain('Runs scripts from 2 skills in a sandbox with no internet access');
    expect(text(el)).toContain('PDF · version 3f2a9c1e · Reviewed by Sam, Nov 23, 2026');
    expect(text(el)).toContain('DOCX · version 3f2a9c1e · Shared by Pat, Nov 23, 2026');
  });

  it('nothing for scripts that do not run for the viewer', () => {
    expect(text(render([status({ state: 'not_allowed' }), status({ state: 'off_by_admin' })]))).toBe('');
  });
});

async function settle(fixture: { detectChanges: () => void }): Promise<void> {
  for (let i = 0; i < 3; i++) {
    await new Promise((resolve) => setTimeout(resolve));
    TestBed.tick();
    fixture.detectChanges();
  }
}

describe('SkillScriptsSwitchComponent (skill-code-sandbox §8.4)', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => '/api' } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function render(scriptsOff: unknown) {
    const fixture = TestBed.createComponent(SkillScriptsSwitchComponent);
    fixture.componentRef.setInput('skillId', 'pdf');
    fixture.detectChanges();
    await settle(fixture);
    http.expectOne('/api/skills/pdf/script-trust').flush({ scriptsOff });
    await settle(fixture);
    return fixture;
  }

  it('turns the scripts off for everyone, and says so on the switch', async () => {
    const fixture = await render(null);
    const el = fixture.nativeElement as HTMLElement;
    const toggle = el.querySelector<HTMLButtonElement>('button[role="switch"]')!;
    expect(toggle.getAttribute('aria-checked')).toBe('true');
    expect(text(el)).toContain('Scripts can run');

    toggle.click();
    const req = http.expectOne('/api/skills/mine/pdf/scripts');
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual({ off: true });
    req.flush({ off: true, by: 'author' });
    await settle(fixture);
    expect(toggle.getAttribute('aria-checked')).toBe('false');
  });

  it("an administrator's switch replaces the toggle with a line saying so", async () => {
    const fixture = await render({ by: 'admin', at: '2026-11-20T09:00:00Z' });
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('button[role="switch"]')).toBeNull();
    expect(text(el)).toBe("An administrator turned off this skill's scripts on Nov 20, 2026.");
  });
});
