import { provideHttpClient } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { WorkflowsPage } from './workflows.page';
import { WorkflowSummary } from './workflow.models';
import { WorkflowsApiService } from '../services/workflows/workflows-api.service';

function summary(overrides: Partial<WorkflowSummary>): WorkflowSummary {
  return { skillId: 'SKL', skillName: 'Carrier', name: 'review', isOwned: true, designerMade: true, title: 'Review', version: 1, stepCount: 2, ...overrides };
}

describe('WorkflowsPage', () => {
  let list: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [WorkflowsPage],
      providers: [provideRouter([]), provideHttpClient()],
    });
    list = vi.spyOn(TestBed.inject(WorkflowsApiService), 'listQuietly') as unknown as ReturnType<typeof vi.fn>;
  });

  async function settle(fixture: ReturnType<typeof TestBed.createComponent<WorkflowsPage>>): Promise<void> {
    for (let i = 0; i < 3; i++) {
      await new Promise((resolve) => setTimeout(resolve));
      await fixture.whenStable();
      fixture.detectChanges();
    }
  }

  async function render(): Promise<HTMLElement> {
    const fixture = TestBed.createComponent(WorkflowsPage);
    fixture.detectChanges();
    await settle(fixture);
    return fixture.nativeElement as HTMLElement;
  }

  it('lists yours first, then the ones you can use, each opening its designer', async () => {
    list.mockResolvedValue([
      summary({ skillId: 'S2', title: 'Granted review', isOwned: false, stepCount: 3, version: 4 }),
      summary({ skillId: 'S1', title: 'My review', stepCount: 4, version: 2 }),
    ]);
    const el = await render();

    const headings = [...el.querySelectorAll('h2')].map((h) => h.textContent!.replace(/\s+/g, ' ').trim());
    expect(headings).toEqual(['Yours 1', 'You can use 1']);
    const links = [...el.querySelectorAll<HTMLAnchorElement>('li a')];
    expect(links.map((a) => a.textContent!.trim())).toEqual(['My review', 'Granted review']);
    expect(links[0].getAttribute('href')).toBe('/workflows/S1/review');
    expect(el.textContent).toContain('4 steps · version 2');
  });

  it('says why a workflow whose file no longer parses can’t be read', async () => {
    list.mockResolvedValue([summary({ title: undefined, stepCount: undefined, version: undefined, problem: 'a step reads itself' })]);
    const el = await render();
    expect(el.textContent).toContain('Can’t be read: a step reads itself');
  });

  it('offers New, from blank or the template, and no Share', async () => {
    list.mockResolvedValue([]);
    const el = await render();
    const hrefs = [...el.querySelectorAll<HTMLAnchorElement>('a')].map((a) => a.getAttribute('href'));
    expect(hrefs).toContain('/workflows/new');
    expect(hrefs).toContain('/workflows/new?template=proposal-review');
    expect(el.textContent).toContain('No workflows yet');
    expect(el.textContent).not.toMatch(/share/i);
  });

  it('shows the kept list at once on a return visit, and refreshes it behind that copy', async () => {
    list.mockResolvedValue([summary({ title: 'My review' })]);
    const first = TestBed.createComponent(WorkflowsPage);
    first.detectChanges();
    await settle(first);
    first.destroy();

    let answer!: (value: WorkflowSummary[]) => void;
    list.mockReturnValue(new Promise<WorkflowSummary[]>((resolve) => (answer = resolve)));
    const again = TestBed.createComponent(WorkflowsPage);
    again.detectChanges();
    const el = again.nativeElement as HTMLElement;
    expect(el.textContent).not.toContain('Loading workflows');
    expect(el.textContent).toContain('My review');
    expect(list).toHaveBeenCalledTimes(2);

    answer([summary({ title: 'My review, renamed' })]);
    await settle(again);
    expect(el.textContent).toContain('My review, renamed');
  });
});
