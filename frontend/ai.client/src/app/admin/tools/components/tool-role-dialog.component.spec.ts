import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../../testing/cdk-dialog';
import { AdminToolService } from '../services/admin-tool.service';
import { AppRolesService } from '../../roles/services/app-roles.service';
import { AdminTool } from '../models/admin-tool.model';
import {
  ToolRoleDialogComponent,
  ToolRoleDialogData,
  ToolRoleDialogResult,
} from './tool-role-dialog.component';

describe('ToolRoleDialogComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: AdminToolService, useValue: { getToolRoles: vi.fn().mockResolvedValue([]) } },
        { provide: AppRolesService, useValue: { fetchRoles: vi.fn().mockResolvedValue({ roles: [] }) } },
      ],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  it('names the dialog from its title and describes it', async () => {
    const tool = { toolId: 'web_search', displayName: 'Web Search', isPublic: false } as AdminTool;
    const { container } = await openInCdkDialog<ToolRoleDialogComponent, ToolRoleDialogData, ToolRoleDialogResult>(
      ToolRoleDialogComponent,
      { data: { tool } },
    );
    expectNamedDialog(container, {
      name: 'Manage Role Access',
      description: 'Select which roles should have access to Web Search.',
    });
  });
});
