import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import { AppRolesService } from '../../roles/services/app-roles.service';
import { MarketplaceDefaultPinsPage } from './default-pins.page';
import { LOCK_WARN_THRESHOLD, RoleAgentPinsResponse } from '../models/marketplace.model';

/**
 * Locked-seed friction (#748).
 *
 * The decision was **friction, not a cap**, so what has to hold is that the cost of
 * locking is visible — not that anything is refused. Two facts carry it: how many seeds
 * this role locks, and how many every *other* role locks. The second is the one an admin
 * cannot work out from this page, and it is the only honest way to show the union
 * problem: a member's shelf is the union across the roles they match, and a lock from any
 * one role wins, so no per-role number describes what an individual ends up with.
 */
describe('MarketplaceDefaultPinsPage — locked-seed friction', () => {
  let mockService: any;
  let mockRoles: any;

  function response(overrides: Partial<RoleAgentPinsResponse> = {}): RoleAgentPinsResponse {
    return {
      roleId: 'faculty',
      roleLabel: 'Faculty',
      fallbackOnly: false,
      unmapped: false,
      lockedElsewhere: 0,
      lockedElsewhereRoles: 0,
      pins: [],
      unavailable: [],
      ...overrides,
    };
  }

  function pin(agentId: string, locked: boolean) {
    return {
      agentId,
      name: agentId,
      category: 'Administration',
      order: 0,
      locked,
      reachable: true,
      visibility: 'PUBLIC',
      state: 'ready' as const,
      missing: [],
      notes: [],
    };
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockService = {
      error: vi.fn().mockReturnValue(null),
      loadListings: vi.fn().mockResolvedValue([]),
      loadRolePins: vi.fn().mockResolvedValue(response()),
      saveRolePins: vi.fn().mockResolvedValue(response()),
    };
    mockRoles = { fetchRoles: vi.fn().mockResolvedValue([{ roleId: 'faculty', displayName: 'Faculty' }]) };

    TestBed.configureTestingModule({
      providers: [
        { provide: AdminMarketplaceService, useValue: mockService },
        { provide: AppRolesService, useValue: mockRoles },
        { provide: Dialog, useValue: { open: vi.fn() } },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  async function render(
    res: RoleAgentPinsResponse,
  ): Promise<ComponentFixture<MarketplaceDefaultPinsPage>> {
    mockService.loadRolePins.mockResolvedValue(res);
    const fixture = TestBed.createComponent(MarketplaceDefaultPinsPage);
    const page = fixture.componentInstance;
    page.roleId.set('faculty');
    await page.reloadPins();
    fixture.detectChanges();
    return fixture;
  }

  function text(fixture: ComponentFixture<unknown>): string {
    return (fixture.nativeElement as HTMLElement).textContent ?? '';
  }

  it('counts the locked seeds on this role', async () => {
    const fixture = await render(
      response({ pins: [pin('a', true), pin('b', false), pin('c', true)] }),
    );
    expect(fixture.componentInstance.lockedCount()).toBe(2);
  });

  it('stays quiet at or below the threshold', async () => {
    const pins = Array.from({ length: LOCK_WARN_THRESHOLD }, (_, i) => pin(`a${i}`, true));
    const fixture = await render(response({ pins }));

    expect(fixture.componentInstance.lockWarning()).toBe(false);
    expect(text(fixture)).not.toContain('Members cannot remove a locked agent');
  });

  it('warns once past the threshold', async () => {
    const pins = Array.from({ length: LOCK_WARN_THRESHOLD + 1 }, (_, i) => pin(`a${i}`, true));
    const fixture = await render(response({ pins }));

    expect(fixture.componentInstance.lockWarning()).toBe(true);
    expect(text(fixture)).toContain('Members cannot remove a locked agent');
  });

  it('never refuses a lock — this is friction, not a cap', async () => {
    const pins = Array.from({ length: 12 }, (_, i) => pin(`a${i}`, true));
    const fixture = await render(response({ pins }));

    // Warned, but every seed is still staged and saveable.
    expect(fixture.componentInstance.lockWarning()).toBe(true);
    expect(fixture.componentInstance.lockedCount()).toBe(12);
    expect(text(fixture)).not.toContain('cannot lock');
  });

  it('surfaces what other roles lock, even when this role locks nothing', async () => {
    // The union case: this role looks innocent, the member's shelf is not.
    const fixture = await render(
      response({ pins: [pin('a', false)], lockedElsewhere: 4, lockedElsewhereRoles: 2 }),
    );

    expect(fixture.componentInstance.lockWarning()).toBe(false);
    const body = text(fixture);
    expect(body).toContain('2 other roles lock');
    expect(body).toContain('4 more');
    expect(body).toContain('union');
  });

  it('says "role locks" rather than "roles lock" for a single other role', async () => {
    const fixture = await render(response({ lockedElsewhere: 1, lockedElsewhereRoles: 1 }));
    expect(text(fixture)).toContain('1 other role locks');
  });

  it('shows nothing when no role locks anything', async () => {
    const fixture = await render(response({ pins: [pin('a', false)] }));
    const body = text(fixture);

    expect(body).not.toContain('other roles lock');
    expect(body).not.toContain('Members cannot remove a locked agent');
  });
});
