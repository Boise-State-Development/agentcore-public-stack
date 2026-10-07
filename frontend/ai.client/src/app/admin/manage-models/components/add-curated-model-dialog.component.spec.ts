import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../../testing/cdk-dialog';
import { AppRolesService } from '../../roles/services/app-roles.service';
import { AppRole } from '../../roles/models/app-role.model';
import { CuratedModel } from '../models/curated-models';
import {
  AddCuratedModelDialogComponent,
  AddCuratedModelDialogData,
  AddCuratedModelDialogResult,
} from './add-curated-model-dialog.component';

describe('AddCuratedModelDialogComponent', () => {
  const roles = [
    { roleId: 'staff', displayName: 'Staff', description: 'All staff' },
    { roleId: 'faculty', displayName: 'Faculty', description: 'All faculty' },
  ] as AppRole[];

  const model = { template: { modelName: 'Claude Opus 5.5' } } as CuratedModel;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        {
          provide: AppRolesService,
          useValue: {
            rolesResource: { isLoading: () => false, error: () => undefined },
            getEnabledRoles: () => roles,
          },
        },
      ],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  function open() {
    return openInCdkDialog<AddCuratedModelDialogComponent, AddCuratedModelDialogData, AddCuratedModelDialogResult>(
      AddCuratedModelDialogComponent,
      { data: { model } },
    );
  }

  it('names the dialog from the model, describes it, and starts on the first role', async () => {
    const { container } = await open();
    expectNamedDialog(container, {
      name: 'Add Claude Opus 5.5',
      description: /^Select which roles can access this model/,
    });
    const initial = container.querySelectorAll('[cdkFocusInitial]');
    expect(initial.length).toBe(1);
    expect(initial[0].textContent?.trim()).toBe('Staff');
  });

  it('closes with the selected role ids, or undefined on Escape', async () => {
    const confirm = await open();
    let result: AddCuratedModelDialogResult | 'open' = 'open';
    confirm.ref.closed.subscribe(r => (result = r));
    confirm.ref.componentInstance!.toggleRole('faculty');
    confirm.ref.componentInstance!.confirm();
    expect(result).toEqual(['faculty']);

    const cancel = await open();
    result = 'open';
    cancel.ref.closed.subscribe(r => (result = r));
    cancel.container
      .querySelector('app-dialog-shell')!
      .dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(result).toBeUndefined();
  });
});
