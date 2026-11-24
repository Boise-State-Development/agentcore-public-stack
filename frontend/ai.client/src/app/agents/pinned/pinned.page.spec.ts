import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { signal } from '@angular/core';
import { AgentPinnedPage } from './pinned.page';
import { AgentPinService } from '../services/agent-pin.service';
import { PinnedAgent } from '../models/store.model';
import { FEATURES } from '../../services/features';
import { environment } from '../../../environments/environment';
import { ConfigService } from '../../services/config.service';

const pin = (agentId: string, name: string, locked = false): PinnedAgent => ({
  agentId,
  name,
  category: 'Teaching',
  source: locked ? 'role' : 'user',
  locked,
});

describe('AgentPinnedPage arranging', () => {
  let organizeLists = true;
  const pins = signal<PinnedAgent[]>([]);
  const service = {
    pins,
    loading: signal(false),
    error: signal<string | null>(null),
    load: vi.fn().mockResolvedValue([]),
    reorder: vi.fn().mockResolvedValue(undefined),
    isPinned: () => true,
    isLocked: (id: string) => pins().some((p) => p.agentId === id && p.locked),
    toggle: vi.fn(),
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    organizeLists = true;
    service.reorder.mockClear();
    pins.set([pin('ast-lock', 'Department Helper', true), pin('ast-a', 'Alpha'), pin('ast-b', 'Beta')]);
    TestBed.configureTestingModule({
      imports: [AgentPinnedPage],
      providers: [
        provideRouter([]),
        { provide: AgentPinService, useValue: service },
        { provide: ConfigService, useValue: { appApiUrl: () => '' } },
        { provide: FEATURES, useFactory: () => ({ ...environment.features, organizeLists }) },
      ],
    });
  });

  const arrangeButton = (el: HTMLElement) => [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Arrange');

  it('offers Arrange only with the flag on and two movable pins', () => {
    let fixture = TestBed.createComponent(AgentPinnedPage);
    fixture.detectChanges();
    expect(arrangeButton(fixture.nativeElement)).toBeTruthy();

    pins.set([pin('ast-lock', 'Department Helper', true), pin('ast-a', 'Alpha')]);
    fixture = TestBed.createComponent(AgentPinnedPage);
    fixture.detectChanges();
    expect(arrangeButton(fixture.nativeElement)).toBeUndefined();
  });

  it('offers nothing while the flag is off', () => {
    organizeLists = false;
    const fixture = TestBed.createComponent(AgentPinnedPage);
    fixture.detectChanges();
    expect(arrangeButton(fixture.nativeElement)).toBeUndefined();
  });

  it('keeps the locked pin first and saves the rest in the arranged order', async () => {
    const fixture = TestBed.createComponent(AgentPinnedPage);
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    arrangeButton(el)!.click();
    fixture.detectChanges();

    expect(el.textContent).toContain('Pinned for your role and kept first');
    expect(el.querySelectorAll('app-arrange-list input[type=checkbox]')).toHaveLength(0);
    el.querySelector<HTMLButtonElement>('button[aria-label="Move Beta up"]')!.click();
    fixture.detectChanges();
    [...el.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Done')!.click();
    await fixture.whenStable();

    expect(service.reorder).toHaveBeenCalledWith(['ast-b', 'ast-a']);
  });
});
