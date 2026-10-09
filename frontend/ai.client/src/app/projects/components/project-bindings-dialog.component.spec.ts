import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { of } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BindableItem } from '../../agents/models/agent.model';
import { ToastService } from '../../services/toast/toast.service';
import { ProjectApiService } from '../services/project-api.service';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';
import {
  ProjectBindingsDialogComponent,
  ProjectBindingsDialogData,
  ProjectBindingsDialogResult,
} from './project-bindings-dialog.component';

function bindable(ref: string, label: string, description: string, category?: string): BindableItem {
  return { kind: 'tool', ref, label, description, meta: category ? { category } : {} };
}

const PALETTE = {
  tools: [
    bindable('calendar', 'Calendar', 'Read your schedule', 'account'),
    bindable('browse_web', 'Web Browser', 'Open and read pages', 'browser'),
  ],
  skills: [bindable('rubric', 'Rubric Builder', 'Builds rubrics')],
};

describe('ProjectBindingsDialogComponent', () => {
  const api = {
    saveBindings: vi.fn((_id: string, _kind: string, bindings: { ref: string }[]) =>
      of({ bindings, version: 3, canEdit: true }),
    ),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    TestBed.configureTestingModule({
      providers: [
        { provide: ProjectApiService, useValue: api },
        { provide: ToastService, useValue: { success: vi.fn(), error: vi.fn() } },
      ],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  async function open(data: Partial<ProjectBindingsDialogData> = {}) {
    const opened = await openInCdkDialog<
      ProjectBindingsDialogComponent,
      ProjectBindingsDialogData,
      ProjectBindingsDialogResult
    >(ProjectBindingsDialogComponent, {
      data: {
        projectId: 'p1',
        canEdit: true,
        bound: { tools: ['calendar', 'gone'], skills: [] },
        palette: PALETTE,
        ...data,
      },
    });
    return opened;
  }

  const checkboxFor = (container: HTMLElement, name: string): HTMLInputElement => {
    const label = [...container.querySelectorAll('[id$="-name"]')].find((n) => n.textContent!.trim() === name)!;
    return container.querySelector(`input[aria-labelledby="${label.id}"]`) as HTMLInputElement;
  };
  const button = (container: HTMLElement, text: string): HTMLButtonElement =>
    [...container.querySelectorAll('button')].find((b) => b.textContent!.trim() === text) as HTMLButtonElement;
  const settle = async () => {
    TestBed.tick();
    await new Promise((resolve) => setTimeout(resolve, 0));
  };

  it('is a named dialog with one shared selector per kind, named by its heading', async () => {
    const { container } = await open();
    expectNamedDialog(container, { name: 'Tools & skills' });
    expect(container.querySelectorAll('app-tool-selector')).toHaveLength(2);
    for (const kind of ['tools', 'skills']) {
      const list = container.querySelector(`[role="group"][aria-labelledby="bindings-${kind}"]`)!;
      expect(list).not.toBeNull();
      // The section blurb (e.g. "Members without access to a tool…") describes the list.
      expect(list.getAttribute('aria-describedby')).toBe(`bindings-${kind}-blurb`);
    }
    expect(container.textContent).toContain('Members without access to a tool get the assistant without it.');
  });

  it('keeps a binding the caller cannot use, locked and explained', async () => {
    const { container } = await open();
    const gone = checkboxFor(container, 'gone');
    expect(gone.checked).toBe(true);
    expect(gone.disabled).toBe(true);
    expect(container.textContent).toContain('Added by someone else.');
  });

  it('saves only the kind that changed, in palette order, keeping the locked binding', async () => {
    const { container, ref } = await open();
    const closed = vi.fn();
    ref.closed.subscribe(closed);

    checkboxFor(container, 'Web Browser').click();
    await settle();
    button(container, 'Save').click();
    await settle();

    expect(api.saveBindings).toHaveBeenCalledTimes(1);
    expect(api.saveBindings).toHaveBeenCalledWith('p1', 'tools', [
      { ref: 'calendar' },
      { ref: 'browse_web' },
      { ref: 'gone' },
    ]);
    expect(closed).toHaveBeenCalled();
  });

  it('keeps Save disabled until something changes', async () => {
    const { container } = await open();
    expect(button(container, 'Save').disabled).toBe(true);
    checkboxFor(container, 'Rubric Builder').click();
    await settle();
    expect(button(container, 'Save').disabled).toBe(false);
  });

  it('is read-only for a viewer', async () => {
    const { container } = await open({ canEdit: false });
    const boxes = [...container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')];
    expect(boxes.length).toBe(4);
    expect(boxes.every((b) => b.disabled)).toBe(true);
    expect(button(container, 'Save')).toBeUndefined();
    expect(button(container, 'Select all')).toBeUndefined();
    expect(button(container, 'Close')).toBeDefined();
  });
});
