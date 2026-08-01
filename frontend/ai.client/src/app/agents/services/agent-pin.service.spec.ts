import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpClient } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { AgentPinService } from './agent-pin.service';
import { ConfigService } from '../../services/config.service';
import { PinnedAgent } from '../models/store.model';

function pin(agentId: string, name = agentId): PinnedAgent {
  return {
    agentId,
    name,
    category: 'Teaching',
    source: 'user',
    locked: false,
  };
}

/**
 * The pin service backs three surfaces at once (the Pinned tab, every Discover row's
 * `＋`, the detail page's Add), so the cases that matter are the ones where those three
 * could disagree: a cached load, an optimistic write, and a rollback.
 */
describe('AgentPinService', () => {
  let service: AgentPinService;
  let http: { get: ReturnType<typeof vi.fn>; post: ReturnType<typeof vi.fn>; delete: ReturnType<typeof vi.fn> };

  beforeEach(() => {
    TestBed.resetTestingModule();
    http = { get: vi.fn(), post: vi.fn(), delete: vi.fn() };

    TestBed.configureTestingModule({
      providers: [
        AgentPinService,
        { provide: HttpClient, useValue: http },
        { provide: ConfigService, useValue: { appApiUrl: () => '/api' } },
      ],
    });
    service = TestBed.inject(AgentPinService);
  });

  it('loads the effective pin list once per session', async () => {
    http.get.mockReturnValue(of({ pins: [pin('ast-001')] }));

    await service.load();
    await service.load();

    expect(http.get).toHaveBeenCalledTimes(1);
    expect(service.pins().map((p) => p.agentId)).toEqual(['ast-001']);
  });

  it('re-reads when forced', async () => {
    http.get.mockReturnValue(of({ pins: [] }));

    await service.load();
    await service.load(true);

    expect(http.get).toHaveBeenCalledTimes(2);
  });

  it('leaves the store browsable when the pin read fails', async () => {
    http.get.mockReturnValue(throwError(() => ({ error: { detail: 'nope' } })));

    const pins = await service.load();

    expect(pins).toEqual([]);
    expect(service.error()).toBe('nope');
  });

  it('answers isPinned from the loaded set', async () => {
    http.get.mockReturnValue(of({ pins: [pin('ast-001')] }));
    await service.load();

    expect(service.isPinned('ast-001')).toBe(true);
    expect(service.isPinned('ast-002')).toBe(false);
  });

  it('adds the server row rather than an optimistic guess', async () => {
    const row = { ...pin('ast-001', 'Policy Lookup'), tagline: 'From the server' };
    http.post.mockReturnValue(of(row));

    await service.pin('ast-001');

    expect(http.post).toHaveBeenCalledWith('/api/agents/ast-001/pin', {});
    expect(service.pins()[0].tagline).toBe('From the server');
  });

  it('does not re-request a pin it already holds', async () => {
    http.get.mockReturnValue(of({ pins: [pin('ast-001')] }));
    await service.load();

    await service.pin('ast-001');

    expect(http.post).not.toHaveBeenCalled();
  });

  it('rolls the list back when a pin fails', async () => {
    http.get.mockReturnValue(of({ pins: [pin('ast-001')] }));
    await service.load();
    http.post.mockReturnValue(throwError(() => ({ error: { detail: 'Too many pins.' } })));

    await expect(service.pin('ast-002')).rejects.toBeTruthy();

    expect(service.pins().map((p) => p.agentId)).toEqual(['ast-001']);
    expect(service.error()).toBe('Too many pins.');
  });

  it('removes optimistically', async () => {
    http.get.mockReturnValue(of({ pins: [pin('ast-001'), pin('ast-002')] }));
    await service.load();
    http.delete.mockReturnValue(of(null));

    await service.unpin('ast-001');

    expect(http.delete).toHaveBeenCalledWith('/api/agents/ast-001/pin');
    expect(service.pins().map((p) => p.agentId)).toEqual(['ast-002']);
  });

  it('restores the row when a removal fails', async () => {
    http.get.mockReturnValue(of({ pins: [pin('ast-001')] }));
    await service.load();
    http.delete.mockReturnValue(throwError(() => ({ status: 500 })));

    await expect(service.unpin('ast-001')).rejects.toBeTruthy();

    expect(service.pins().map((p) => p.agentId)).toEqual(['ast-001']);
  });

  it('toggles in both directions', async () => {
    http.get.mockReturnValue(of({ pins: [] }));
    await service.load();
    http.post.mockReturnValue(of(pin('ast-001')));
    http.delete.mockReturnValue(of(null));

    await service.toggle('ast-001');
    expect(service.isPinned('ast-001')).toBe(true);

    await service.toggle('ast-001');
    expect(service.isPinned('ast-001')).toBe(false);
  });

  // ── role-seeded pins (D9, Phase 6) ─────────────────────────────────────────────
  it('reports a locked role pin as locked', async () => {
    http.get.mockReturnValue(
      of({ pins: [{ ...pin('ast-locked'), source: 'role', locked: true }, pin('ast-own')] }),
    );

    await service.load();

    expect(service.isLocked('ast-locked')).toBe(true);
    expect(service.isLocked('ast-own')).toBe(false);
  });

  it('refuses to toggle a locked role pin', async () => {
    // The server no-ops the DELETE too (D9.4); refusing here keeps the row from
    // flickering out and back on the next read.
    http.get.mockReturnValue(
      of({ pins: [{ ...pin('ast-locked'), source: 'role', locked: true }] }),
    );
    await service.load();

    await service.toggle('ast-locked');

    expect(http.delete).not.toHaveBeenCalled();
    expect(service.isPinned('ast-locked')).toBe(true);
  });

  it('lets an unlocked role pin be dismissed like any other', async () => {
    http.get.mockReturnValue(of({ pins: [{ ...pin('ast-seeded'), source: 'role' }] }));
    await service.load();
    http.delete.mockReturnValue(of(null));

    await service.toggle('ast-seeded');

    expect(http.delete).toHaveBeenCalledWith('/api/agents/ast-seeded/pin');
    expect(service.isPinned('ast-seeded')).toBe(false);
  });
});
