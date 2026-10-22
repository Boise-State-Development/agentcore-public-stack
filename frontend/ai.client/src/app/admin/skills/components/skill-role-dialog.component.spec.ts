import { describe, it, beforeEach, afterEach, vi } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../../testing/cdk-dialog';
import { AdminSkillService } from '../services/admin-skill.service';
import { AppRolesService } from '../../roles/services/app-roles.service';
import { AdminSkill } from '../models/admin-skill.model';
import {
  SkillRoleDialogComponent,
  SkillRoleDialogData,
  SkillRoleDialogResult,
} from './skill-role-dialog.component';

describe('SkillRoleDialogComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: AdminSkillService, useValue: { getSkillRoles: vi.fn().mockResolvedValue([]) } },
        { provide: AppRolesService, useValue: { fetchRoles: vi.fn().mockResolvedValue({ roles: [] }) } },
      ],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  it('names the dialog from its title and describes it', async () => {
    const skill = { skillId: 'brand-deck', displayName: 'Brand Deck' } as AdminSkill;
    const { container } = await openInCdkDialog<SkillRoleDialogComponent, SkillRoleDialogData, SkillRoleDialogResult>(
      SkillRoleDialogComponent,
      { data: { skill } },
    );
    expectNamedDialog(container, {
      name: 'Manage Role Access',
      description: 'Select which roles can use Brand Deck.',
    });
  });
});
