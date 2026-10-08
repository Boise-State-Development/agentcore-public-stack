import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { MemoryReviewComponent } from './memory-review.component';
import { ProjectApiService } from '../services/project-api.service';
import { ToastService } from '../../services/toast/toast.service';
import { MemoryProposal, MemoryProposalDetail, Project } from '../models/project.model';
import { MEMORY_PROJECT as PROJECT, memoryEntry } from '../../../testing/project-memory.fixtures';

const PROPOSAL: MemoryProposal = {
  proposalId: 'p1',
  state: 'pending',
  slug: 'sis',
  text: '- Term codes are YYYYTT. <!-- e:aaaaaaaa -->\n- New fact, see [[rates]].',
  baseVersion: 2,
  proposedByEmail: 'vi@x.edu',
  proposedByName: 'Vi Viewer',
  proposerKind: 'agent',
  createdAt: '2026-10-07T12:00:00Z',
  edited: false,
  isMine: false,
  stale: false,
};

const COMPACTION: MemoryProposal = {
  ...PROPOSAL,
  proposalId: 'c1',
  kind: 'compaction',
  slug: 'canvas',
  proposerKind: 'maintenance',
  text: '- Batch calls in groups of 50. <!-- e:aaaaaaaa -->',
  ops: [
    {
      type: 'merge',
      sources: [{ anchor: 'aaaaaaaa', text: 'Batch calls in groups of 50.' }, { anchor: 'bbbbbbbb', text: 'Calls go in batches of 50.' }],
      text: 'Batch calls in groups of 50.',
      keep: 'aaaaaaaa',
      why: 'The same rule twice',
    },
    { type: 'prune', sources: [{ anchor: 'cccccccc', text: 'Kickoff on 2026-09-14.' }], reason: 'expired', why: 'The meeting happened' },
    {
      type: 'supersede',
      sources: [{ anchor: 'dddddddd', text: 'Owner: Marcus.' }, { anchor: 'eeeeeeee', text: 'Owner: Priya.' }],
      why: 'Priya took over',
    },
  ],
  runId: 'r1',
};

const DETAIL: MemoryProposalDetail = {
  ...PROPOSAL,
  currentText: '- Term codes are YYYYTT. <!-- e:aaaaaaaa -->\n- Old fact. <!-- e:bbbbbbbb -->',
};

describe('MemoryReviewComponent', () => {
  const api = {
    proposals: vi.fn(),
    proposal: vi.fn(),
    approveProposal: vi.fn(),
    rejectProposal: vi.fn(),
    withdrawProposal: vi.fn(),
  };
  const toast = { success: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.proposals.mockReturnValue(of({ proposals: [PROPOSAL] }));
    api.proposal.mockReturnValue(of(DETAIL));
    api.approveProposal.mockReturnValue(of({ ...PROPOSAL, state: 'approved' }));
    api.rejectProposal.mockReturnValue(of({ ...PROPOSAL, state: 'rejected' }));
    api.withdrawProposal.mockReturnValue(of({ ...PROPOSAL, state: 'withdrawn' }));
    TestBed.configureTestingModule({
      imports: [MemoryReviewComponent],
      providers: [
        { provide: ProjectApiService, useValue: api },
        { provide: ToastService, useValue: toast },
        provideRouter([]),
      ],
    });
  });

  async function render(project: Project = PROJECT) {
    const fixture = TestBed.createComponent(MemoryReviewComponent);
    fixture.componentRef.setInput('project', project);
    fixture.componentRef.setInput('entries', [memoryEntry('sis'), memoryEntry('rates')]);
    const decided = vi.fn();
    fixture.componentInstance.decided.subscribe(decided);
    fixture.detectChanges();
    for (let i = 0; i < 4; i++) {
      await fixture.whenStable();
      await new Promise(r => setTimeout(r, 0));
      fixture.detectChanges();
    }
    const el = fixture.nativeElement as HTMLElement;
    const button = (label: RegExp) =>
      Array.from(el.querySelectorAll('button')).find(b => label.test(b.textContent ?? '')) as HTMLButtonElement;
    const settle = async () => {
      for (let i = 0; i < 4; i++) {
        await fixture.whenStable();
        await new Promise(r => setTimeout(r, 0));
        fixture.detectChanges();
      }
    };
    const column = (name: string) =>
      Array.from(el.querySelectorAll('section'))
        .find(s => s.querySelector('h4')?.textContent?.trim() === name)!
        .querySelectorAll('li');
    return { fixture, el, button, settle, column, decided };
  }

  it('opens the first pending proposal with the current and proposed file side by side', async () => {
    const { el, column } = await render();
    expect(api.proposals).toHaveBeenCalledWith('prj_1', 'pending');
    expect(api.proposal).toHaveBeenCalledWith('prj_1', 'p1');
    expect(el.textContent).toContain('Review queue');
    expect(el.textContent).toContain('Vi Viewer, through the assistant');
    expect(el.textContent).toContain('This adds 1 item and removes 1 item.');
    const text = (li: Element) => li.textContent?.replace(/\s+/g, ' ').trim();
    expect(Array.from(column('Current')).map(text)).toEqual(['Term codes are YYYYTT.', 'Removed: Old fact.']);
    expect(Array.from(column('Proposed')).map(text)).toEqual(['Term codes are YYYYTT.', 'Added: New fact, see rates.']);
    expect(el.querySelector('section a')?.getAttribute('href')).toContain('file=rates');
  });

  it('approves with a note and tells the page the file changed', async () => {
    const { el, button, settle, decided } = await render();
    const note = el.querySelector<HTMLInputElement>('#proposal-note-p1')!;
    note.value = 'Thanks';
    note.dispatchEvent(new Event('input'));
    button(/^\s*Approve\s*$/).click();
    await settle();

    expect(api.approveProposal).toHaveBeenCalledWith('prj_1', 'p1', { note: 'Thanks' });
    expect(toast.success).toHaveBeenCalledWith('Saved “sis” to project memory.');
    expect(decided).toHaveBeenCalledWith({ proposal: { ...PROPOSAL, state: 'approved' }, applied: true });
    expect(el.textContent).toContain('All caught up');
  });

  it('a stale proposal can only be approved after editing, and sends the edited text', async () => {
    api.proposals.mockReturnValue(of({ proposals: [{ ...PROPOSAL, stale: true }] }));
    api.proposal.mockReturnValue(of({ ...DETAIL, stale: true }));
    const { el, fixture, button, settle } = await render();
    expect(el.textContent).toContain('File changed since');
    expect(el.textContent).toContain('changed “sis” after this was proposed');
    expect(button(/^\s*Approve\s*$/).disabled).toBe(true);

    button(/Edit before approving/).click();
    fixture.detectChanges();
    const area = el.querySelector<HTMLTextAreaElement>('#proposal-text-p1')!;
    area.value = '- Edited.';
    area.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    expect(el.textContent).toContain('Your edited version');
    button(/Approve edited version/).click();
    await settle();

    expect(api.approveProposal).toHaveBeenCalledWith('prj_1', 'p1', { text: '- Edited.', note: undefined });
  });

  it('declines, and shows the API sentence when someone decided first', async () => {
    api.rejectProposal.mockReturnValue(
      throwError(() => new HttpErrorResponse({ status: 409, error: { detail: 'Someone else decided this proposal first.' } })),
    );
    const { el, button, settle } = await render();
    button(/Decline/).click();
    await settle();
    expect(el.textContent).toContain('Someone else decided this proposal first.');
  });

  it('a viewer sees their own proposals and can withdraw, not decide', async () => {
    api.proposals.mockReturnValue(of({ proposals: [{ ...PROPOSAL, isMine: true }] }));
    api.proposal.mockReturnValue(of({ ...DETAIL, isMine: true }));
    const { el, button, settle, decided } = await render({ ...PROJECT, role: 'viewer' });
    expect(el.textContent).toContain('Your proposals');
    expect(button(/Decline/)).toBeUndefined();
    button(/Withdraw/).click();
    await settle();
    expect(api.withdrawProposal).toHaveBeenCalledWith('prj_1', 'p1');
    expect(decided).toHaveBeenCalledWith(expect.objectContaining({ applied: false }));
  });

  it('an archived project offers no decisions', async () => {
    const { button } = await render({ ...PROJECT, status: 'archived' });
    expect(button(/Approve/)).toBeUndefined();
  });

  it('moves to the next proposal after a decision', async () => {
    api.proposals.mockReturnValue(of({ proposals: [PROPOSAL, { ...PROPOSAL, proposalId: 'p2', slug: 'rates' }] }));
    const { button, settle } = await render();
    button(/Decline/).click();
    await settle();
    expect(api.proposal).toHaveBeenLastCalledWith('prj_1', 'p2');
  });

  describe('a maintenance proposal', () => {
    beforeEach(() => {
      api.proposals.mockReturnValue(of({ proposals: [COMPACTION] }));
      api.proposal.mockReturnValue(of({ ...COMPACTION, currentText: null }));
      api.approveProposal.mockReturnValue(of({ ...COMPACTION, state: 'approved', appliedOps: [0, 1, 2] }));
    });

    it('lists each change with its reason and what it removes, all chosen', async () => {
      const { el, button } = await render();
      expect(el.textContent).toContain('Tidy up');
      expect(el.textContent).toContain('Suggested by maintenance Vi Viewer ran');
      expect(el.textContent).toContain('Merge 2 items that say the same thing');
      expect(el.textContent).toContain('The meeting happened');
      expect(el.textContent).toContain('Kept, and replaces it:');
      expect(el.querySelectorAll('section h4').length).toBe(0);
      const boxes = el.querySelectorAll<HTMLInputElement>('input[type="checkbox"]');
      expect(Array.from(boxes).map(b => b.checked)).toEqual([true, true, true]);
      expect(button(/Apply 3 changes/)).toBeDefined();
      expect(button(/Edit before approving/)).toBeUndefined();
    });

    it('applies every change with no ops list, or only the ones left ticked', async () => {
      const { el, button, settle, decided } = await render();
      button(/Apply 3 changes/).click();
      await settle();
      expect(api.approveProposal).toHaveBeenCalledWith('prj_1', 'c1', { note: undefined });
      expect(toast.success).toHaveBeenCalledWith('Applied 3 changes to “canvas”.');
      expect(decided).toHaveBeenCalledWith(expect.objectContaining({ applied: true }));

      api.proposals.mockReturnValue(of({ proposals: [COMPACTION] }));
      const second = await render();
      second.el.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')[1].click();
      await second.settle();
      expect(second.el.textContent).toContain('Not chosen: this item stays in the file as it is.');
      api.approveProposal.mockReturnValue(of({ ...COMPACTION, state: 'approved', appliedOps: [0] }));
      second.button(/Apply 2 changes/).click();
      await second.settle();
      expect(api.approveProposal).toHaveBeenLastCalledWith('prj_1', 'c1', { note: undefined, ops: [0, 2] });
      expect(toast.success).toHaveBeenLastCalledWith('Applied 1 change to “canvas”. The others no longer matched the file.');
      void el;
    });

    it('when none of its changes still apply, only declining is left', async () => {
      api.proposal.mockReturnValue(of({ ...COMPACTION, stale: true, currentText: null }));
      const { el, button } = await render();
      expect(el.textContent).toContain('none of these changes still apply');
      expect(button(/Apply 3 changes/).disabled).toBe(true);
      expect(button(/Decline/).disabled).toBe(false);
    });
  });
});
