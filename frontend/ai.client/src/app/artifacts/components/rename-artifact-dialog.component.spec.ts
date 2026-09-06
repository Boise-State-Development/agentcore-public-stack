import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';

import {
  MAX_ARTIFACT_TITLE_LENGTH,
  RenameArtifactDialogComponent,
  type RenameArtifactDialogData,
} from './rename-artifact-dialog.component';

describe('RenameArtifactDialogComponent', () => {
  let close: ReturnType<typeof vi.fn>;

  function create(data: RenameArtifactDialogData) {
    TestBed.resetTestingModule();
    close = vi.fn();
    TestBed.configureTestingModule({
      providers: [
        { provide: DIALOG_DATA, useValue: data },
        { provide: DialogRef, useValue: { close } },
      ],
    });
    const fixture = TestBed.createComponent(RenameArtifactDialogComponent);
    fixture.detectChanges();
    return {
      fixture,
      // Reaching protected members the way the template does.
      api: fixture.componentInstance as unknown as {
        draft: { set: (v: string) => void };
        canConfirm: () => boolean;
        tooLong: () => boolean;
        confirm: () => void;
        onCancel: () => void;
        onEnter: () => void;
      },
    };
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.restoreAllMocks();
  });

  it('starts pre-filled with the current title', () => {
    const { fixture } = create({ title: 'Quarterly plan' });
    const input: HTMLInputElement =
      fixture.nativeElement.querySelector('#rename-artifact-input');
    expect(input.value).toBe('Quarterly plan');
  });

  it('cannot confirm an unchanged title', () => {
    // A stray Enter must not fire a write that renames every version row
    // to exactly what it already says.
    const { api } = create({ title: 'Quarterly plan' });
    expect(api.canConfirm()).toBe(false);
  });

  it('cannot confirm a whitespace-only title', () => {
    const { api } = create({ title: 'Quarterly plan' });
    api.draft.set('   ');
    expect(api.canConfirm()).toBe(false);
  });

  it('cannot confirm past the length cap, and says why', () => {
    const { api } = create({ title: 'Quarterly plan' });
    api.draft.set('x'.repeat(MAX_ARTIFACT_TITLE_LENGTH + 1));
    expect(api.tooLong()).toBe(true);
    expect(api.canConfirm()).toBe(false);
  });

  it('closes with the trimmed title', () => {
    const { api } = create({ title: 'Old' });
    api.draft.set('  New name  ');
    api.confirm();
    expect(close).toHaveBeenCalledWith('New name');
  });

  it('closes with undefined on cancel, meaning "cancelled"', () => {
    const { api } = create({ title: 'Old' });
    api.onCancel();
    expect(close).toHaveBeenCalledWith(undefined);
  });

  it('ignores Enter while the title is not confirmable', () => {
    const { api } = create({ title: 'Old' });
    api.onEnter();
    expect(close).not.toHaveBeenCalled();
  });
});
