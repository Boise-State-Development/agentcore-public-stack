import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AgentService } from '../../services/agent.service';
import { AgentSkillPinsComponent } from './agent-skill-pins.component';

describe('AgentSkillPinsComponent', () => {
  afterEach(() => TestBed.resetTestingModule());

  async function render(service: Partial<Record<keyof AgentService, unknown>>) {
    TestBed.configureTestingModule({
      imports: [AgentSkillPinsComponent],
      providers: [{ provide: AgentService, useValue: service }],
    });
    const fixture = TestBed.createComponent(AgentSkillPinsComponent);
    fixture.componentRef.setInput('agentId', 'agt-1');
    fixture.componentRef.setInput('labels', { redline: 'Contract Redline', fresh: 'Fresh' });
    const emitted: unknown[] = [];
    fixture.componentInstance.pinned.subscribe((m) => emitted.push(m));
    const el = fixture.nativeElement as HTMLElement;
    const settle = async () => {
      for (let i = 0; i < 3; i++) {
        await new Promise((resolve) => setTimeout(resolve));
        await fixture.whenStable();
        fixture.detectChanges();
      }
    };
    fixture.detectChanges();
    await settle();
    return { el, settle, emitted };
  }

  it('lists only pinned skills with a newer version, and moves one on Update', async () => {
    const updateSkillPin = vi.fn().mockResolvedValue(8);
    const r = await render({
      getSkillPins: vi.fn().mockResolvedValue([
        { skillId: 'redline', version: 2, updateAvailable: true },
        { skillId: 'fresh', version: 4, updateAvailable: false },
      ]),
      updateSkillPin,
    });
    expect(r.el.textContent).toContain('Contract Redline');
    expect(r.el.textContent).not.toContain('Fresh');

    r.el.querySelector<HTMLButtonElement>('button[aria-label="Update Contract Redline"]')!.click();
    await r.settle();
    expect(updateSkillPin).toHaveBeenCalledWith('agt-1', 'redline', {});
    expect(r.emitted).toEqual([{ skillId: 'redline', version: 8 }]);
    expect(r.el.textContent).toContain('Contract Redline now uses version 8.');
    expect(r.el.querySelector('button[aria-label="Update Contract Redline"]')).toBeNull();
  });

  it('shows nothing when no pin is behind, or when the read fails', async () => {
    const quiet = await render({
      getSkillPins: vi.fn().mockResolvedValue([{ skillId: 'fresh', version: 4, updateAvailable: false }]),
    });
    expect(quiet.el.querySelector('section')).toBeNull();
    TestBed.resetTestingModule();
    const failed = await render({ getSkillPins: vi.fn().mockRejectedValue(new Error('404')) });
    expect(failed.el.querySelector('section')).toBeNull();
  });

  it("shows the backend's reason when an update is refused", async () => {
    const r = await render({
      getSkillPins: vi.fn().mockResolvedValue([{ skillId: 'redline', version: 2, updateAvailable: true }]),
      updateSkillPin: vi.fn().mockRejectedValue({ error: { detail: 'You no longer have this skill' } }),
    });
    r.el.querySelector<HTMLButtonElement>('button[aria-label="Update Contract Redline"]')!.click();
    await r.settle();
    expect(r.el.querySelector('[role="alert"]')?.textContent).toContain('You no longer have this skill');
    expect(r.emitted).toEqual([]);
  });
});
