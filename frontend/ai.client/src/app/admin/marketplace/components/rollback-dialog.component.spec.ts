import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';

import { RollbackDialogComponent } from './rollback-dialog.component';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import { AdminListingRow, AgentVersionSummary } from '../models/marketplace.model';

/**
 * The rollback picker (§8).
 *
 * Two rules worth pinning, because getting either wrong makes the dialog offer a choice the
 * backend then refuses: the currently-published version is never selectable, and a reason is
 * mandatory — it is what the author sees when the thing they wrote gets replaced.
 */
describe('RollbackDialogComponent', () => {
  let closed: unknown;
  let mockService: { loadVersions: ReturnType<typeof vi.fn> };

  const listing = {
    agentId: 'ast-001',
    name: 'Policy Lookup',
    ownerName: 'Ada Author',
    category: 'Administration',
    state: 'published',
    publishedVersion: 3,
    usageCount: 0,
    updatedAt: '2026-07-22T00:00:00Z',
    reachability: 'everyone',
    adminEdits: [],
  } as AdminListingRow;

  function version(n: number, isPublished = false): AgentVersionSummary {
    return {
      version: n,
      name: `Policy Lookup v${n}`,
      createdAt: '2026-07-0' + n + 'T00:00:00Z',
      isPublished,
    };
  }

  function build(versions: AgentVersionSummary[]): RollbackDialogComponent {
    closed = 'not-closed';
    mockService = { loadVersions: vi.fn().mockResolvedValue({ versions, publishedVersion: 3 }) };
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        {
          provide: DialogRef,
          useValue: {
            close: (v: unknown) => {
              closed = v;
            },
          },
        },
        { provide: DIALOG_DATA, useValue: { listing } },
        { provide: AdminMarketplaceService, useValue: mockService },
      ],
    });
    return TestBed.createComponent(RollbackDialogComponent).componentInstance;
  }

  afterEach(() => TestBed.resetTestingModule());

  beforeEach(() => {
    closed = 'not-closed';
  });

  it('offers every version except the one already live', async () => {
    const component = build([version(3, true), version(2), version(1)]);
    await component.ngOnInit();

    expect(component.selectable().map((v) => v.version)).toEqual([2, 1]);
  });

  it('says so plainly when there is nothing else to switch to', async () => {
    const component = build([version(3, true)]);
    await component.ngOnInit();

    expect(component.selectable()).toEqual([]);
    expect(component.canSubmit()).toBe(false);
  });

  it('offers later versions too, so a rollback can be undone', async () => {
    // Serving v1 with v2–v3 still on the shelf: nothing was deleted, so the picker's job is
    // "everything that is not live", not "everything below the live one". Filtering by
    // direction would make the sequel to every rollback unreachable.
    const component = build([version(3), version(2), version(1, true)]);
    await component.ngOnInit();

    expect(component.selectable().map((v) => v.version)).toEqual([3, 2]);
  });

  it('will not submit without a reason', async () => {
    // The author is about to have what they wrote replaced; a silent pointer move leaves
    // them with a store tile that no longer matches their last approval and no explanation.
    const component = build([version(3, true), version(2)]);
    await component.ngOnInit();
    component.version.set(2);

    expect(component.canSubmit()).toBe(false);
    component.reason.set('   ');
    expect(component.canSubmit()).toBe(false);
    component.reason.set('v3 broke citations.');
    expect(component.canSubmit()).toBe(true);
  });

  it('closes with the version and trimmed reason', async () => {
    const component = build([version(3, true), version(2)]);
    await component.ngOnInit();
    component.version.set(2);
    component.reason.set('  v3 broke citations.  ');
    component.onSubmit();

    expect(closed).toEqual({ version: 2, reason: 'v3 broke citations.' });
  });

  it('closes with undefined on cancel, which the caller reads as "do nothing"', async () => {
    const component = build([version(3, true), version(2)]);
    await component.ngOnInit();
    component.onCancel();

    expect(closed).toBeUndefined();
  });

  it('surfaces a load failure instead of rendering an empty picker', async () => {
    const component = build([]);
    mockService.loadVersions.mockRejectedValue({ error: { detail: 'Agent not found.' } });
    await component.ngOnInit();

    expect(component.error()).toBe('Agent not found.');
    expect(component.loading()).toBe(false);
  });
});
