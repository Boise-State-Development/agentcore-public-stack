import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { ProjectApiService } from './project-api.service';
import { ProjectsService, isUnavailable, projectErrorMessage } from './projects.service';
import { Project } from '../models/project.model';

const project = (id: string, extra: Partial<Project> = {}): Project => ({
  projectId: id,
  name: `Project ${id}`,
  description: '',
  ownerEmail: 'o@x.edu',
  role: 'owner',
  status: 'active',
  editorsManageMembers: true,
  memberCount: 0,
  harnessAgentId: `ast-${id}`,
  createdAt: '2026-09-24T00:00:00Z',
  updatedAt: '2026-09-24T00:00:00Z',
  ...extra,
});

describe('ProjectsService', () => {
  const api = { list: vi.fn(), create: vi.fn() };
  let service: ProjectsService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    TestBed.configureTestingModule({ providers: [{ provide: ProjectApiService, useValue: api }] });
    service = TestBed.inject(ProjectsService);
  });

  it('loads the list and marks the feature available', async () => {
    api.list.mockReturnValue(of({ projects: [project('a'), project('b')] }));
    await service.load();
    expect(service.projects$().map(p => p.projectId)).toEqual(['a', 'b']);
    expect(service.available$()).toBe(true);
    expect(service.error$()).toBeNull();
  });

  it('reads a 404 as "switched off here", not as an error', async () => {
    api.list.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 404 })));
    await service.load();
    expect(service.available$()).toBe(false);
    expect(service.error$()).toBeNull();
  });

  it('shows the backend’s own sentence for other failures', async () => {
    api.list.mockReturnValue(
      throwError(() => new HttpErrorResponse({ status: 500, error: { detail: 'Projects are having a moment.' } })),
    );
    await service.load();
    expect(service.error$()).toBe('Projects are having a moment.');
    expect(service.available$()).toBeNull();
  });

  it('puts a new or changed project first and drops a removed one', async () => {
    api.list.mockReturnValue(of({ projects: [project('a'), project('b')] }));
    await service.load();
    api.create.mockReturnValue(of(project('c')));
    await service.create('C', '');
    service.upsert(project('b', { name: 'Renamed' }));
    service.remove('a');
    expect(service.projects$().map(p => [p.projectId, p.name])).toEqual([['b', 'Renamed'], ['c', 'Project c']]);
  });
});

describe('projectErrorMessage', () => {
  it('prefers the API detail, then a connection message, then the fallback', () => {
    expect(projectErrorMessage(new HttpErrorResponse({ status: 409, error: { detail: 'Archived.' } }))).toBe('Archived.');
    expect(projectErrorMessage(new HttpErrorResponse({ status: 0 }))).toContain('Can’t reach the server');
    expect(projectErrorMessage(new Error('x'), 'Fallback.')).toBe('Fallback.');
    expect(isUnavailable(new HttpErrorResponse({ status: 404 }))).toBe(true);
    expect(isUnavailable(new HttpErrorResponse({ status: 403 }))).toBe(false);
  });
});
