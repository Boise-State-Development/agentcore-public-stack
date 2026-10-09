import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';
import { ProjectsService } from '../services/projects.service';
import { CreateProjectDialogComponent, CreateProjectDialogResult } from './create-project-dialog.component';

describe('CreateProjectDialogComponent', () => {
  let create: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    TestBed.resetTestingModule();
    create = vi.fn(async (name: string) => ({ projectId: 'p1', name }));
    TestBed.configureTestingModule({
      providers: [{ provide: ProjectsService, useValue: { create } }],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  function open() {
    return openInCdkDialog<CreateProjectDialogComponent, unknown, CreateProjectDialogResult>(CreateProjectDialogComponent);
  }

  it('names the dialog from its title, describes it, and starts on the name field', async () => {
    const { container } = await open();
    expectNamedDialog(container, { name: 'New project', description: /^A shared space with its own instructions/ });
    expect(container.querySelector('[cdkFocusInitial]')?.id).toBe('project-name');
  });

  it('submits the form from the footer button', async () => {
    const { ref, container } = await open();
    let result: CreateProjectDialogResult;
    ref.closed.subscribe(r => (result = r));

    const name = container.querySelector<HTMLInputElement>('#project-name')!;
    name.value = 'Canvas sync';
    name.dispatchEvent(new Event('input'));
    container.querySelector<HTMLFormElement>('#create-project-form')!.dispatchEvent(new Event('submit'));
    await new Promise(resolve => setTimeout(resolve, 0));

    expect(create).toHaveBeenCalledWith('Canvas sync', '');
    expect(result!).toEqual({ projectId: 'p1', name: 'Canvas sync' });
  });
});
