import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../../testing/cdk-dialog';
import { RoleOptionsService } from '../../roles/services/role-options.service';
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

  function provideRoles(service: Pick<RoleOptionsService, 'rolesResource' | 'getEnabledRoles'>) {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [{ provide: RoleOptionsService, useValue: service }],
    });
  }

  /** As the real resource behaves in error: `error()` holds the response and `value()` throws. */
  function failRoles(status: number) {
    provideRoles({
      rolesResource: {
        isLoading: () => false,
        error: () => new HttpErrorResponse({ status, url: '/admin/roles/picker-options' }),
      } as unknown as RoleOptionsService['rolesResource'],
      getEnabledRoles: () => {
        throw new Error('Resource is currently in an error state');
      },
    });
  }

  beforeEach(() =>
    provideRoles({
      rolesResource: {
        isLoading: () => false,
        error: () => undefined,
      } as unknown as RoleOptionsService['rolesResource'],
      getEnabledRoles: () => roles,
    }),
  );

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

  function addButton(container: HTMLElement): HTMLButtonElement {
    return [...container.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Add to models')!;
  }

  it('keeps Add disabled until a role is picked when roles load', async () => {
    const { ref, container } = await open();
    expect(addButton(container).disabled).toBe(true);
    ref.componentInstance!.toggleRole('staff');
    TestBed.tick();
    expect(addButton(container).disabled).toBe(false);
  });

  // A refused role list (e.g. an SPA ahead of its app-api) must not block the add.
  it('lets an admin who cannot read roles add the model with no roles, and says why', async () => {
    failRoles(403);
    const { ref, container } = await open();
    expectNamedDialog(container, { name: 'Add Claude Opus 5.5' });

    const text = container.textContent!.replace(/\s+/g, ' ');
    expect(text).toContain("You don't have access to the role list");
    expect(text).toContain('someone who manages roles can grant it to specific roles later');
    expect(text).not.toContain('Failed to load roles');

    let result: AddCuratedModelDialogResult | 'open' = 'open';
    ref.closed.subscribe(r => (result = r));
    const add = addButton(container);
    expect(add.disabled).toBe(false);
    add.click();
    expect(result).toEqual([]);
  });

  it('still allows adding with no roles when the role list fails for another reason', async () => {
    failRoles(500);
    const { ref, container } = await open();
    expect(container.textContent).toContain("Roles couldn't be loaded");
    expect(container.textContent).not.toContain("You don't have access to the role list");

    let result: AddCuratedModelDialogResult | 'open' = 'open';
    ref.closed.subscribe(r => (result = r));
    addButton(container).click();
    expect(result).toEqual([]);
  });
});
