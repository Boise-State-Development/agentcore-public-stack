import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { WorkflowsPage } from './workflows.page';
import { WorkflowSummary } from './workflow.models';
import { WorkflowService } from './workflow.service';

function summary(overrides: Partial<WorkflowSummary>): WorkflowSummary {
  return { skillId: 'SKL', skillName: 'Carrier', name: 'review', isOwned: true, designerMade: true, title: 'Review', version: 1, stepCount: 2, ...overrides };
}

describe('WorkflowsPage', () => {
  let list: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    list = vi.fn();
    TestBed.configureTestingModule({
      imports: [WorkflowsPage],
      providers: [provideRouter([]), { provide: WorkflowService, useValue: { list } }],
    });
  });

  async function render(): Promise<HTMLElement> {
    const fixture = TestBed.createComponent(WorkflowsPage);
    fixture.detectChanges();
    for (let i = 0; i < 3; i++) {
      await new Promise((resolve) => setTimeout(resolve));
      await fixture.whenStable();
      fixture.detectChanges();
    }
    return fixture.nativeElement as HTMLElement;
  }

  it('lists yours first, then the ones you can use, each opening its designer', async () => {
    list.mockResolvedValue({
      workflows: [
        summary({ skillId: 'S2', title: 'Granted review', isOwned: false, stepCount: 3, version: 4 }),
        summary({ skillId: 'S1', title: 'My review', stepCount: 4, version: 2 }),
      ],
    });
    const el = await render();

    const headings = [...el.querySelectorAll('h2')].map((h) => h.textContent!.replace(/\s+/g, ' ').trim());
    expect(headings).toEqual(['Yours 1', 'You can use 1']);
    const links = [...el.querySelectorAll<HTMLAnchorElement>('li a')];
    expect(links.map((a) => a.textContent!.trim())).toEqual(['My review', 'Granted review']);
    expect(links[0].getAttribute('href')).toBe('/workflows/S1/review');
    expect(el.textContent).toContain('4 steps · version 2');
  });

  it('says why a workflow whose file no longer parses can’t be read', async () => {
    list.mockResolvedValue({ workflows: [summary({ title: undefined, stepCount: undefined, version: undefined, problem: 'a step reads itself' })] });
    const el = await render();
    expect(el.textContent).toContain('Can’t be read: a step reads itself');
  });

  it('offers New, from blank or the template, and no Share', async () => {
    list.mockResolvedValue({ workflows: [] });
    const el = await render();
    const hrefs = [...el.querySelectorAll<HTMLAnchorElement>('a')].map((a) => a.getAttribute('href'));
    expect(hrefs).toContain('/workflows/new');
    expect(hrefs).toContain('/workflows/new?template=proposal-review');
    expect(el.textContent).toContain('No workflows yet');
    expect(el.textContent).not.toMatch(/share/i);
  });
});
