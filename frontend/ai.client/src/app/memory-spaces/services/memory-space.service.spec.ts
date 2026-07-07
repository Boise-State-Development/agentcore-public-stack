import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { MemorySpaceService } from './memory-space.service';
import { MemorySpaceApiService } from './memory-space-api.service';
import { MemorySpaceSummary, SpaceTemplate } from '../models/memory-space.model';

function stubSpace(overrides: Partial<MemorySpaceSummary> = {}): MemorySpaceSummary {
  return {
    spaceId: 'spc_abc123',
    name: 'My Brain',
    template: 'chief-of-staff',
    role: 'owner',
    ownerId: 'user-1',
    createdAt: '2026-07-07T00:00:00Z',
    updatedAt: '2026-07-07T00:00:00Z',
    ...overrides,
  };
}

const TEMPLATES: SpaceTemplate[] = [
  { templateId: 'blank', name: 'Blank', description: 'An empty wiki' },
];

describe('MemorySpaceService', () => {
  let service: MemorySpaceService;
  let mockApi: {
    list: ReturnType<typeof vi.fn>;
    create: ReturnType<typeof vi.fn>;
    get: ReturnType<typeof vi.fn>;
    remove: ReturnType<typeof vi.fn>;
    export: ReturnType<typeof vi.fn>;
    updateIndex: ReturnType<typeof vi.fn>;
    readEntry: ReturnType<typeof vi.fn>;
    upsertEntry: ReturnType<typeof vi.fn>;
    deleteEntry: ReturnType<typeof vi.fn>;
    listShares: ReturnType<typeof vi.fn>;
    addShare: ReturnType<typeof vi.fn>;
    updateShare: ReturnType<typeof vi.fn>;
    removeShare: ReturnType<typeof vi.fn>;
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockApi = {
      list: vi.fn(),
      create: vi.fn(),
      get: vi.fn(),
      remove: vi.fn(),
      export: vi.fn(),
      updateIndex: vi.fn(),
      readEntry: vi.fn(),
      upsertEntry: vi.fn(),
      deleteEntry: vi.fn(),
      listShares: vi.fn(),
      addShare: vi.fn(),
      updateShare: vi.fn(),
      removeShare: vi.fn(),
    };
    TestBed.configureTestingModule({
      providers: [MemorySpaceService, { provide: MemorySpaceApiService, useValue: mockApi }],
    });
    service = TestBed.inject(MemorySpaceService);
  });

  it('loads spaces and templates and marks the feature accessible', async () => {
    const space = stubSpace();
    mockApi.list.mockReturnValue(of({ spaces: [space], templates: TEMPLATES }));

    await service.loadSpaces();

    expect(service.spaces$()).toEqual([space]);
    expect(service.templates$()).toEqual(TEMPLATES);
    expect(service.accessible$()).toBe(true);
    expect(service.loading$()).toBe(false);
  });

  it('marks the feature inaccessible on a 404 (kill switch off) without an error', async () => {
    mockApi.list.mockReturnValue(throwError(() => ({ status: 404 })));

    await service.loadSpaces();

    expect(service.accessible$()).toBe(false);
    expect(service.spaces$()).toEqual([]);
    expect(service.error$()).toBeNull();
  });

  it('surfaces a real error for non-gating failures', async () => {
    mockApi.list.mockReturnValue(throwError(() => new Error('network blip')));

    await expect(service.loadSpaces()).rejects.toThrow('network blip');
    expect(service.error$()).toBe('network blip');
    // accessible stays null (unresolved) so the nav entry doesn't flash in
    expect(service.accessible$()).toBeNull();
  });

  it('appends a created space to local state', async () => {
    mockApi.list.mockReturnValue(of({ spaces: [], templates: TEMPLATES }));
    await service.loadSpaces();

    const created = stubSpace({ spaceId: 'spc_new', name: 'Research' });
    mockApi.create.mockReturnValue(of(created));

    const result = await service.createSpace({ name: 'Research', template: 'blank' });

    expect(result).toEqual(created);
    expect(service.spaces$()).toContain(created);
  });

  it('removes a space from local state on delete/leave', async () => {
    const space = stubSpace();
    mockApi.list.mockReturnValue(of({ spaces: [space], templates: [] }));
    await service.loadSpaces();

    mockApi.remove.mockReturnValue(of(undefined));
    await service.deleteOrLeave(space.spaceId);

    expect(service.spaces$()).toEqual([]);
  });

  it('returns the export blob', async () => {
    const blob = new Blob(['zip-bytes'], { type: 'application/zip' });
    mockApi.export.mockReturnValue(of(blob));

    const result = await service.exportSpace('spc_abc123');

    expect(result).toBe(blob);
    expect(mockApi.export).toHaveBeenCalledWith('spc_abc123');
  });

  it('delegates share operations to the api', async () => {
    mockApi.addShare.mockReturnValue(of({ email: 'a@b.edu', permission: 'viewer', createdAt: '' }));
    mockApi.updateShare.mockReturnValue(of({ email: 'a@b.edu', permission: 'editor', createdAt: '' }));
    mockApi.removeShare.mockReturnValue(of(undefined));

    await service.addShare('spc_1', { email: 'a@b.edu', permission: 'viewer' });
    await service.updateShare('spc_1', 'a@b.edu', 'editor');
    await service.removeShare('spc_1', 'a@b.edu');

    expect(mockApi.addShare).toHaveBeenCalledWith('spc_1', { email: 'a@b.edu', permission: 'viewer' });
    expect(mockApi.updateShare).toHaveBeenCalledWith('spc_1', 'a@b.edu', 'editor');
    expect(mockApi.removeShare).toHaveBeenCalledWith('spc_1', 'a@b.edu');
  });
});
