import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';
import { MemorySpaceService } from '../services/memory-space.service';
import {
  CreateSpaceDialogComponent,
  CreateSpaceDialogData,
  CreateSpaceDialogResult,
} from './create-space-dialog.component';

describe('CreateSpaceDialogComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [{ provide: MemorySpaceService, useValue: { createSpace: vi.fn() } }],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  function open() {
    return openInCdkDialog<CreateSpaceDialogComponent, CreateSpaceDialogData, CreateSpaceDialogResult>(
      CreateSpaceDialogComponent,
      { data: { templates: [{ templateId: 'blank', name: 'Blank', description: '' }] } },
    );
  }

  it('names the dialog from its title, describes it, and starts on the name field', async () => {
    const { container } = await open();
    expectNamedDialog(container, { name: 'New memory space', description: /second brain/ });
    expect(container.querySelector('[cdkFocusInitial]')?.id).toBe('space-name');
  });

  it('closes undefined on Escape', async () => {
    const { ref, container } = await open();
    let result: CreateSpaceDialogResult | null = null;
    ref.closed.subscribe(r => (result = r));
    container
      .querySelector('app-dialog-shell')!
      .dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(result).toBeUndefined();
  });
});
