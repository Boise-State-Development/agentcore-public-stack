import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { of, throwError } from 'rxjs';
import { ProjectOutputsComponent } from './project-outputs.component';
import { ProjectApiService } from '../services/project-api.service';
import { Project, ProjectOutput, ProjectSchedule } from '../models/project.model';

const PROJECT: Project = {
  projectId: 'prj_1',
  name: 'Enrollment Sync',
  description: '',
  ownerEmail: 'o@x.edu',
  ownerName: null,
  role: 'viewer',
  status: 'active',
  editorsManageMembers: true,
  memberCount: 3,
  harnessAgentId: 'ast-1',
  createdAt: '2026-09-24T00:00:00Z',
  updatedAt: '2026-09-24T00:00:00Z',
};

const THEIRS: ProjectOutput = {
  artifactId: 'art-1',
  shareId: 'sh-1',
  version: 2,
  title: 'Roster chart',
  contentType: 'text/html; charset=utf-8',
  sharedByEmail: 'ann@x.edu',
  sharedByName: 'Ann Lee',
  sharedAt: '2026-10-07T12:00:00Z',
  shareUrl: '/shared-artifact/sh-1',
  isMine: false,
  canRemove: false,
};
const MINE: ProjectOutput = {
  ...THEIRS, artifactId: 'art-2', shareId: 'sh-2', version: 1, title: 'Term dates', contentType: 'text/csv',
  sharedByEmail: 'me@x.edu', sharedByName: null, isMine: true, canRemove: true,
};

const SCHEDULE = {
  scheduleId: 'sc_1',
  label: 'Weekly digest',
  cadence: 'weekly',
  hourLocal: 8,
  weekday: 0,
  timezone: 'America/Boise',
  state: 'active',
  nextRunAt: '2026-10-19T14:00:00Z',
} as ProjectSchedule;

describe('ProjectOutputsComponent', () => {
  const api = { outputs: vi.fn(), removeOutput: vi.fn(), schedule: vi.fn() };
  const dialog = { open: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.outputs.mockReturnValue(of({ outputs: [THEIRS, MINE] }));
    api.removeOutput.mockReturnValue(of(undefined));
    api.schedule.mockReturnValue(of(SCHEDULE));
    dialog.open.mockReturnValue({ closed: of(true) });
    TestBed.configureTestingModule({
      imports: [ProjectOutputsComponent],
      providers: [
        provideRouter([]),
        { provide: ProjectApiService, useValue: api },
        { provide: Dialog, useValue: dialog },
      ],
    });
  });

  async function render(project: Project = PROJECT) {
    const fixture = TestBed.createComponent(ProjectOutputsComponent);
    fixture.componentRef.setInput('project', project);
    fixture.detectChanges();
    // Twice: the outputs, then the featured output's schedule read after them.
    for (let i = 0; i < 2; i++) {
      await fixture.whenStable();
      fixture.detectChanges();
    }
    return { fixture, el: fixture.nativeElement as HTMLElement };
  }

  it('lists outputs as cards that open the shared-artifact view', async () => {
    const { el } = await render();
    expect(api.outputs).toHaveBeenCalledWith('prj_1');
    expect(el.querySelector('h2')?.textContent).toContain('Outputs');
    const links = Array.from(el.querySelectorAll('a'));
    expect(links.map(a => a.getAttribute('href'))).toEqual(['/shared-artifact/sh-1', '/shared-artifact/sh-2']);
    expect(links.map(a => a.getAttribute('aria-label'))).toEqual([
      'Open Web page Roster chart, shared by Ann Lee',
      'Open CSV Term dates, shared by you',
    ]);
    const [theirs, mine] = links.map(a => a.textContent?.replace(/\s+/g, ' ') ?? '');
    expect(theirs).toContain('Roster chart');
    expect(theirs).toContain('Web page');
    expect(theirs).toContain('v2');
    expect(theirs).toContain('Shared by Ann Lee');
    expect(theirs).toContain('· Oct 7');
    expect(mine).toContain('CSV');
    expect(mine).not.toContain('v1');
    expect(mine).toContain('Shared by you');
    expect(el.querySelector('[data-testid="featured-output"]')).toBeNull();
    expect(api.schedule).not.toHaveBeenCalled();
  });

  it('draws each card from its stored excerpt', async () => {
    api.outputs.mockReturnValue(of({ outputs: [{ ...MINE, preview: 'term,starts\nFall,Aug 24' }, THEIRS] }));
    const { el } = await render();
    const [csv, page] = Array.from(el.querySelectorAll('li'));
    expect(csv.querySelector('[data-testid="preview-csv"] th')?.textContent).toBe('term');
    expect(page.querySelector('app-artifact-preview')?.getAttribute('aria-hidden')).toBe('true');
    expect(page.querySelector('[data-testid="preview-page"]')).toBeNull();
  });

  it('fans out a stack behind an output with several versions', async () => {
    const { el } = await render();
    const [theirs, mine] = Array.from(el.querySelectorAll('li'));
    expect(theirs.querySelectorAll(':scope > div[aria-hidden="true"]').length).toBe(2);
    expect(mine.querySelectorAll(':scope > div[aria-hidden="true"]').length).toBe(0);
  });

  it('leads with the newest scheduled output and its schedule', async () => {
    const scheduled = { ...THEIRS, scheduleId: 'sc_1' };
    api.outputs.mockReturnValue(of({ outputs: [MINE, scheduled] }));
    const { el } = await render();
    expect(api.schedule).toHaveBeenCalledWith('prj_1', 'sc_1');
    const featured = el.querySelector('[data-testid="featured-output"]') as HTMLElement;
    expect(featured.querySelector('a')?.getAttribute('aria-label')).toBe('Open Web page Roster chart, scheduled, shared by Ann Lee');
    expect(featured.textContent).toContain('Latest scheduled run');
    expect(featured.querySelector('[data-testid="schedule-cadence"]')?.textContent).toContain('Every Monday at 8:00 AM');
    expect(featured.querySelector('[data-testid="schedule-next"]')?.textContent).toContain('Next run Mon, Oct 19');
    const sheets = Array.from(el.querySelectorAll('li a')).map(a => a.getAttribute('href'));
    expect(sheets).toEqual(['/shared-artifact/sh-2']);
  });

  it('reads a paused schedule as paused, and a scheduled card without its schedule still reads', async () => {
    api.outputs.mockReturnValue(of({ outputs: [{ ...THEIRS, scheduleId: 'sc_1' }] }));
    api.schedule.mockReturnValue(of({ ...SCHEDULE, state: 'paused' }));
    let { el } = await render();
    expect(el.querySelector('[data-testid="schedule-next"]')?.textContent).toContain('Paused');

    api.schedule.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 404 })));
    ({ el } = await render());
    const featured = el.querySelector('[data-testid="featured-output"]') as HTMLElement;
    expect(featured.textContent).toContain('Published Oct 7');
    expect(featured.querySelector('[data-testid="schedule-cadence"]')).toBeNull();
    expect(el.querySelector('[role=alert]')).toBeNull();
  });

  it('tags later scheduled outputs on their sheets', async () => {
    api.outputs.mockReturnValue(of({ outputs: [{ ...THEIRS, scheduleId: 'sc_1' }, { ...MINE, scheduleId: 'sc_2' }] }));
    const { el } = await render();
    const [sheet] = Array.from(el.querySelectorAll('li'));
    expect(sheet.querySelector('[data-testid="scheduled-tag"]')?.textContent?.trim()).toBe('Scheduled');
    expect(sheet.querySelector('a')?.getAttribute('aria-label')).toBe('Open CSV Term dates, scheduled, shared by you');
  });

  it('offers removal only where the caller may remove', async () => {
    const { el } = await render();
    const buttons = Array.from(el.querySelectorAll('button')).map(b => b.getAttribute('aria-label'));
    expect(buttons).toEqual(['Stop sharing Term dates']);
  });

  it('removes after confirmation', async () => {
    const { el, fixture } = await render();
    (el.querySelector('button[aria-label="Stop sharing Term dates"]') as HTMLButtonElement).click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(api.removeOutput).toHaveBeenCalledWith('prj_1', 'art-2');
    expect(Array.from(el.querySelectorAll('a')).length).toBe(1);
  });

  it('does nothing when the confirmation is cancelled', async () => {
    dialog.open.mockReturnValue({ closed: of(undefined) });
    const { el, fixture } = await render();
    (el.querySelector('button[aria-label="Stop sharing Term dates"]') as HTMLButtonElement).click();
    await fixture.whenStable();
    expect(api.removeOutput).not.toHaveBeenCalled();
  });

  it('renders nothing until something is shared', async () => {
    api.outputs.mockReturnValue(of({ outputs: [] }));
    const { el } = await render();
    expect(el.querySelector('section')).toBeNull();
  });

  it('shows the API sentence when the list fails', async () => {
    api.outputs.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 500, error: { detail: 'Projects are having trouble.' } })));
    const { el } = await render();
    expect(el.querySelector('[role=alert]')?.textContent).toContain('Projects are having trouble.');
  });
});
