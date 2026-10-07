import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { ProjectProposalsComponent, diffProposal } from './project-proposals.component';
import { ProjectApiService } from '../services/project-api.service';
import { ToastService } from '../../services/toast/toast.service';
import { MemoryProposal, MemoryProposalDetail, Project } from '../models/project.model';

const PROJECT: Project = {
  projectId: 'prj_1',
  name: 'Enrollment Sync',
  description: '',
  ownerEmail: 'o@x.edu',
  ownerName: null,
  role: 'editor',
  status: 'active',
  editorsManageMembers: true,
  memberCount: 3,
  harnessAgentId: 'ast-1',
  createdAt: '2026-09-24T00:00:00Z',
  updatedAt: '2026-09-24T00:00:00Z',
};

const PROPOSAL: MemoryProposal = {
  proposalId: 'p1',
  state: 'pending',
  slug: 'sis',
  text: '- Term codes are YYYYTT. <!-- e:aaaaaaaa -->\n- New fact.',
  baseVersion: 2,
  proposedByEmail: 'vi@x.edu',
  proposedByName: 'Vi Viewer',
  proposerKind: 'agent',
  createdAt: '2026-10-07T12:00:00Z',
  edited: false,
  isMine: false,
  stale: false,
};

const DETAIL: MemoryProposalDetail = {
  ...PROPOSAL,
  currentText: '- Term codes are YYYYTT. <!-- e:aaaaaaaa -->\n- Old fact. <!-- e:bbbbbbbb -->',
};

describe('diffProposal', () => {
  it('marks added, removed and kept items, with anchors hidden', () => {
    expect(diffProposal(DETAIL.currentText, PROPOSAL.text)).toEqual([
      { kind: 'same', text: '- Term codes are YYYYTT.' },
      { kind: 'added', text: '- New fact.' },
      { kind: 'removed', text: '- Old fact.' },
    ]);
  });

  it('reads a new file as all added', () => {
    expect(diffProposal(null, '- A.\n- B.').map(l => l.kind)).toEqual(['added', 'added']);
  });
});

describe('ProjectProposalsComponent', () => {
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
      imports: [ProjectProposalsComponent],
      providers: [
        { provide: ProjectApiService, useValue: api },
        { provide: ToastService, useValue: toast },
      ],
    });
  });

  async function render(project: Project = PROJECT) {
    const fixture = TestBed.createComponent(ProjectProposalsComponent);
    fixture.componentRef.setInput('project', project);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const button = (label: RegExp) =>
      Array.from(el.querySelectorAll('button')).find(b => label.test(b.textContent ?? '')) as HTMLButtonElement;
    const open = async () => {
      button(/Change to/).click();
      await fixture.whenStable();
      fixture.detectChanges();
    };
    return { fixture, el, button, open };
  }

  it('lists pending proposals for a reviewer and asks only for pending ones', async () => {
    const { el } = await render();
    expect(api.proposals).toHaveBeenCalledWith('prj_1', 'pending');
    expect(el.textContent).toContain('Proposed memory changes');
    expect(el.textContent).toContain('Change to “sis”');
    expect(el.textContent).toContain('Vi Viewer, through the assistant');
  });

  it('renders nothing when nothing is pending', async () => {
    api.proposals.mockReturnValue(of({ proposals: [] }));
    const { el } = await render();
    expect(el.querySelector('section')).toBeNull();
  });

  it('stays quiet when the project has no shared memory (409)', async () => {
    api.proposals.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 409, error: { detail: 'no memory' } })));
    const { el } = await render();
    expect(el.querySelector('[role=alert]')).toBeNull();
  });

  it('opens a proposal with what it changes, then approves it with a note', async () => {
    const { el, fixture, button, open } = await render();
    await open();
    expect(api.proposal).toHaveBeenCalledWith('prj_1', 'p1');
    const lines = Array.from(el.querySelectorAll('ul[aria-label="What this changes"] li')).map(li => li.textContent?.replace(/\s+/g, ' ').trim());
    expect(lines).toEqual(['Unchanged: - Term codes are YYYYTT.', 'Added: + - New fact.', 'Removed: − - Old fact.']);

    const note = el.querySelector<HTMLInputElement>('#proposal-note-p1')!;
    note.value = 'Thanks';
    note.dispatchEvent(new Event('input'));
    button(/^\s*Approve\s*$/).click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(api.approveProposal).toHaveBeenCalledWith('prj_1', 'p1', { note: 'Thanks' });
    expect(toast.success).toHaveBeenCalledWith('Saved “sis” to project memory.');
    expect(el.querySelector('section')).toBeNull();
  });

  it('a stale proposal can only be approved after editing, and sends the edited text', async () => {
    api.proposals.mockReturnValue(of({ proposals: [{ ...PROPOSAL, stale: true }] }));
    api.proposal.mockReturnValue(of({ ...DETAIL, stale: true }));
    const { el, fixture, button, open } = await render();
    await open();
    expect(el.textContent).toContain('changed “sis” after this was proposed');
    expect(button(/^\s*Approve\s*$/).disabled).toBe(true);

    button(/Edit before approving/).click();
    fixture.detectChanges();
    const area = el.querySelector<HTMLTextAreaElement>('#proposal-text-p1')!;
    area.value = '- Edited.';
    area.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    button(/Approve edited version/).click();
    await fixture.whenStable();

    expect(api.approveProposal).toHaveBeenCalledWith('prj_1', 'p1', { text: '- Edited.', note: undefined });
  });

  it('declines, and shows the API sentence when someone decided first', async () => {
    api.rejectProposal.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 409, error: { detail: 'Someone else decided this proposal first.' } })));
    const { el, fixture, button, open } = await render();
    await open();
    button(/Decline/).click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(el.textContent).toContain('Someone else decided this proposal first.');
  });

  it('a viewer sees their own proposals and can withdraw, not decide', async () => {
    api.proposals.mockReturnValue(of({ proposals: [{ ...PROPOSAL, isMine: true }] }));
    api.proposal.mockReturnValue(of({ ...DETAIL, isMine: true }));
    const { el, fixture, button, open } = await render({ ...PROJECT, role: 'viewer' });
    expect(el.textContent).toContain('Your proposed memory changes');
    await open();
    expect(button(/Decline/)).toBeUndefined();
    button(/Withdraw/).click();
    await fixture.whenStable();
    expect(api.withdrawProposal).toHaveBeenCalledWith('prj_1', 'p1');
  });

  it('an archived project offers no decisions', async () => {
    const { button, open } = await render({ ...PROJECT, status: 'archived' });
    await open();
    expect(button(/Approve/)).toBeUndefined();
  });
});
