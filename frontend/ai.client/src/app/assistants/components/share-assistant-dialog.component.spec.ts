import { describe, it, expect, beforeEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';

import { ShareAssistantDialogComponent } from './share-assistant-dialog.component';
import { AssistantService } from '../services/assistant.service';
import { UserApiService } from '../../users/services/user-api.service';
import { ConfigService } from '../../services/config.service';
import { ShareEntry, Assistant } from '../models/assistant.model';

/**
 * Why a component spec for delta logic instead of a service spec:
 * the dialog owns the diff between "what was loaded" and "what the user
 * pressed Save with". That delta drives which backend endpoints get called
 * (POST share, DELETE unshare, PATCH update). Getting it wrong issues
 * spurious writes — worth a focused unit test.
 *
 * We bypass vi.mock in favor of DI tokens (project convention) so this spec
 * can't leak state into others through the shared worker pool.
 */
describe('ShareAssistantDialogComponent.computeDeltas', () => {
  let component: ShareAssistantDialogComponent;

  const fakeAssistant: Assistant = {
    assistantId: 'ast-test',
    ownerId: 'u1',
    ownerName: 'Alice',
    name: 'Test',
    description: '',
    instructions: '',
    vectorIndexId: 'idx',
    visibility: 'SHARED',
    tags: [],
    starters: [],
    usageCount: 0,
    createdAt: '',
    updatedAt: '',
    status: 'COMPLETE',
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        // ConfigService is pulled in transitively by AssistantApiService;
        // stub the only signal it exposes so the component constructs cleanly.
        { provide: ConfigService, useValue: { appApiUrl: () => 'http://test' } },
        // Replace network-bound services with no-op doubles. We only exercise
        // computeDeltas — none of these are called by it.
        {
          provide: AssistantService,
          useValue: {
            getAssistantShares: async () => [],
            shareAssistant: async () => undefined,
            unshareAssistant: async () => undefined,
            updateSharePermission: async () => undefined,
            updateAssistant: async () => undefined,
          },
        },
        {
          provide: UserApiService,
          useValue: { searchUsers: () => ({ subscribe: () => ({}) }) },
        },
        { provide: DialogRef, useValue: { close: () => undefined } },
        { provide: DIALOG_DATA, useValue: { assistant: fakeAssistant } },
        ShareAssistantDialogComponent,
      ],
    });
    component = TestBed.inject(ShareAssistantDialogComponent);
  });

  it('detects pure additions', () => {
    const initial: ShareEntry[] = [];
    const next: ShareEntry[] = [{ email: 'a@x.com', permission: 'viewer' }];

    const result = (component as any).computeDeltas(initial, next);

    expect(result.adds).toEqual([{ email: 'a@x.com', permission: 'viewer' }]);
    expect(result.removes).toEqual([]);
    expect(result.permissionChanges).toEqual([]);
  });

  it('detects pure removals', () => {
    const initial: ShareEntry[] = [{ email: 'a@x.com', permission: 'viewer' }];
    const next: ShareEntry[] = [];

    const result = (component as any).computeDeltas(initial, next);

    expect(result.adds).toEqual([]);
    expect(result.removes).toEqual(['a@x.com']);
    expect(result.permissionChanges).toEqual([]);
  });

  it('detects a permission upgrade on an already-shared email', () => {
    const initial: ShareEntry[] = [{ email: 'a@x.com', permission: 'viewer' }];
    const next: ShareEntry[] = [{ email: 'a@x.com', permission: 'editor' }];

    const result = (component as any).computeDeltas(initial, next);

    expect(result.adds).toEqual([]);
    expect(result.removes).toEqual([]);
    expect(result.permissionChanges).toEqual([{ email: 'a@x.com', permission: 'editor' }]);
  });

  it('emits no deltas when nothing changed', () => {
    const same: ShareEntry[] = [
      { email: 'a@x.com', permission: 'viewer' },
      { email: 'b@x.com', permission: 'editor' },
    ];

    const result = (component as any).computeDeltas(same, [...same]);

    expect(result.adds).toEqual([]);
    expect(result.removes).toEqual([]);
    expect(result.permissionChanges).toEqual([]);
  });

  it('handles mixed adds, removes, and permission changes in one save', () => {
    const initial: ShareEntry[] = [
      { email: 'keep-viewer@x.com', permission: 'viewer' },
      { email: 'upgrade@x.com', permission: 'viewer' },
      { email: 'remove@x.com', permission: 'editor' },
    ];
    const next: ShareEntry[] = [
      { email: 'keep-viewer@x.com', permission: 'viewer' },
      { email: 'upgrade@x.com', permission: 'editor' },
      { email: 'new@x.com', permission: 'viewer' },
    ];

    const result = (component as any).computeDeltas(initial, next);

    expect(result.adds).toEqual([{ email: 'new@x.com', permission: 'viewer' }]);
    expect(result.removes).toEqual(['remove@x.com']);
    expect(result.permissionChanges).toEqual([
      { email: 'upgrade@x.com', permission: 'editor' },
    ]);
  });
});
