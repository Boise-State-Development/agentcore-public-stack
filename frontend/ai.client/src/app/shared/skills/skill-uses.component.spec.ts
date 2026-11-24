import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';
import { SkillUsesComponent } from './skill-uses.component';
import { SkillUsesDialogComponent, offerPinUpdates } from './skill-uses-dialog.component';
import { SkillUse, SkillUses, SkillUsesApi } from './skill-uses.model';

function use(overrides: Partial<SkillUse>): SkillUse {
  return {
    kind: 'agent',
    id: 'a1',
    name: 'Advisor',
    pinnedVersion: null,
    canUpdate: true,
    shared: false,
    published: false,
    ...overrides,
  };
}

const USES: SkillUses = {
  current: [use({ id: 'a-live', name: 'Live Agent', shared: true })],
  behind: [
    use({ id: 'a-old', name: 'Grader', pinnedVersion: 2, published: true }),
    use({ kind: 'project', id: 'p1', name: 'Grant Office', pinnedVersion: 3 }),
  ],
  othersBehind: 1,
  complete: true,
};

function fakeApi(overrides: Partial<Record<keyof SkillUsesApi, unknown>> = {}): SkillUsesApi {
  return {
    getUses: vi.fn().mockResolvedValue(USES),
    updateUses: vi.fn().mockResolvedValue([]),
    describeError: (_: unknown, fallback: string) => fallback,
    ...overrides,
  } as SkillUsesApi;
}

describe('SkillUsesComponent', () => {
  afterEach(() => TestBed.resetTestingModule());

  async function render(api: SkillUsesApi, inputs: { mode?: 'section' | 'offer'; initial?: SkillUses } = {}) {
    TestBed.configureTestingModule({ imports: [SkillUsesComponent] });
    const fixture = TestBed.createComponent(SkillUsesComponent);
    fixture.componentRef.setInput('api', api);
    fixture.componentRef.setInput('skillId', 'grant_review');
    if (inputs.mode) fixture.componentRef.setInput('mode', inputs.mode);
    if (inputs.initial) fixture.componentRef.setInput('initial', inputs.initial);
    let done = 0;
    fixture.componentInstance.done.subscribe(() => done++);
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
    const button = (label: string) =>
      [...el.querySelectorAll('button')].find((b) => b.textContent?.trim().startsWith(label));
    return { el, settle, button, done: () => done, fixture };
  }

  it('shows the three groups, with every place behind ticked', async () => {
    const r = await render(fakeApi());
    const text = r.el.textContent?.replace(/\s+/g, ' ') ?? '';
    expect(text).toContain("Where it's used");
    expect(text).toContain('Grader · agent · on version 2');
    expect(text).toContain('Grant Office · project · on version 3');
    expect(text).toContain('Updates its draft. Resubmit the agent');
    expect(text).toContain('1 more place pins an older version that isn\'t yours to update.');
    expect(text).toContain('Live Agent · shared agent');
    const boxes = [...r.el.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')];
    expect(boxes.map((b) => b.checked)).toEqual([true, true]);
  });

  it('updates only what is ticked and reports a failure on its row', async () => {
    const updateUses = vi.fn().mockResolvedValue([
      { kind: 'project', id: 'p1', ok: false, error: 'You are not an editor of this project' },
    ]);
    const r = await render(fakeApi({ updateUses }));
    r.el.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
    await r.settle();
    r.button('Update selected')!.click();
    await r.settle();

    expect(updateUses).toHaveBeenCalledWith('grant_review', [{ kind: 'project', id: 'p1' }]);
    expect(r.el.textContent).toContain('You are not an editor of this project');
    expect(r.el.querySelector('[role="status"]')?.textContent).toContain('Updated 0 of 1');
  });

  it('says when it could not check everywhere, or finds nothing', async () => {
    const r = await render(fakeApi({ getUses: vi.fn().mockResolvedValue({ ...USES, complete: false }) }));
    expect(r.el.textContent).toContain("couldn't check every agent and project");
    TestBed.resetTestingModule();
    const empty = await render(
      fakeApi({ getUses: vi.fn().mockResolvedValue({ current: [], behind: [], othersBehind: 0, complete: true }) }),
    );
    expect(empty.el.textContent).toContain('None of your agents or projects use this skill.');
  });

  it('in an offer, opens on the uses it was given and finishes after a clean update or Not now', async () => {
    const getUses = vi.fn();
    const updateUses = vi.fn().mockResolvedValue([
      { kind: 'agent', id: 'a-old', ok: true, version: 9 },
      { kind: 'project', id: 'p1', ok: true, version: 9 },
    ]);
    const r = await render(fakeApi({ getUses, updateUses }), { mode: 'offer', initial: USES });
    expect(getUses).not.toHaveBeenCalled();
    expect(r.el.querySelector('#skill-uses-heading')).toBeNull();
    r.button('Update selected')!.click();
    await r.settle();
    expect(r.done()).toBe(1);

    TestBed.resetTestingModule();
    const later = await render(fakeApi(), { mode: 'offer', initial: USES });
    later.button('Not now')!.click();
    expect(later.done()).toBe(1);
  });
});

describe('offerPinUpdates', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('opens nothing when no place is behind, or when the lookup fails', async () => {
    const dialog = { open: vi.fn() } as unknown as Dialog;
    await offerPinUpdates(
      dialog,
      fakeApi({ getUses: vi.fn().mockResolvedValue({ ...USES, behind: [] }) }),
      'grant_review',
    );
    await offerPinUpdates(dialog, fakeApi({ getUses: vi.fn().mockRejectedValue(new Error('x')) }), 'grant_review');
    expect((dialog as unknown as { open: ReturnType<typeof vi.fn> }).open).not.toHaveBeenCalled();
  });

  it('is a named dialog listing what is behind', async () => {
    TestBed.configureTestingModule({});
    const { container, ref } = await openInCdkDialog(SkillUsesDialogComponent, {
      data: { api: fakeApi(), skillId: 'grant_review', uses: USES },
    });
    expectNamedDialog(container, { name: 'Some places still use an earlier version' });
    expect(container.textContent).toContain('Grant Office');
    ref.close();
  });
});
