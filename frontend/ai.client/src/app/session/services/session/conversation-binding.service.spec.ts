import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, ParamMap } from '@angular/router';
import { HttpErrorResponse } from '@angular/common/http';
import { BehaviorSubject, Subject } from 'rxjs';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Project } from '../../../projects/models/project.model';
import { ProjectApiService } from '../../../projects/services/project-api.service';
import { ProjectsService } from '../../../projects/services/projects.service';
import { FEATURES } from '../../../services/features';
import { ConversationBindingService, bindingQueryParams } from './conversation-binding.service';

function project(overrides: Partial<Project>): Project {
  return {
    projectId: 'p1',
    name: 'Biology 101',
    description: '',
    ownerEmail: 'pat@example.edu',
    ownerName: 'Pat',
    role: 'owner',
    status: 'active',
    editorsManageMembers: false,
    memberCount: 0,
    harnessAgentId: 'harness-1',
    createdAt: '2026-10-01T00:00:00Z',
    updatedAt: '2026-10-01T00:00:00Z',
    ...overrides,
  };
}

const LISTED = project({});

describe('ConversationBindingService', () => {
  let params: BehaviorSubject<ParamMap>;
  let projects: ReturnType<typeof signal<Project[]>>;
  let get: ReturnType<typeof vi.fn>;
  let response: Subject<Project>;

  function build(query: Record<string, string>, opts: { projectsOn?: boolean; list?: Project[] } = {}) {
    params = new BehaviorSubject(convertToParamMap(query));
    projects = signal<Project[]>(opts.list ?? [LISTED]);
    response = new Subject<Project>();
    get = vi.fn(() => response.asObservable());
    TestBed.configureTestingModule({
      providers: [
        { provide: ActivatedRoute, useValue: { queryParamMap: params.asObservable() } },
        { provide: FEATURES, useValue: { projects: opts.projectsOn ?? true } },
        { provide: ProjectsService, useValue: { projects$: projects.asReadonly() } },
        { provide: ProjectApiService, useValue: { get } },
      ],
    });
    const service = TestBed.inject(ConversationBindingService);
    TestBed.tick();
    return service;
  }

  beforeEach(() => TestBed.resetTestingModule());

  describe('a `?project=` URL', () => {
    it('resolves to the project and sends to its harness', () => {
      const service = build({ project: 'p1' });
      expect(service.boundProject()?.name).toBe('Biology 101');
      expect(service.boundAgentId()).toBe('harness-1');
      expect(service.projectPending()).toBe(false);
      expect(get).not.toHaveBeenCalled();
    });

    it('fetches a project the list does not hold, and is pending until it lands', async () => {
      const service = build({ project: 'p2' });
      expect(get).toHaveBeenCalledWith('p2');
      expect(service.projectPending()).toBe(true);
      // Pending is what keeps a first message from going out unbound.
      expect(service.boundAgentId()).toBeNull();

      const resolved = service.resolveBoundAgentId();
      response.next(project({ projectId: 'p2', name: 'Grant proposal', harnessAgentId: 'harness-2' }));
      response.complete();

      expect(await resolved).toBe('harness-2');
      expect(service.projectPending()).toBe(false);
      expect(service.boundProject()?.name).toBe('Grant proposal');
    });

    it('fetches once, however many times it is read', () => {
      const service = build({ project: 'p2' });
      void service.resolveBoundAgentId();
      void service.resolveBoundAgentId();
      TestBed.tick();
      expect(get).toHaveBeenCalledTimes(1);
    });

    it('says why a project could not be resolved, and binds nothing', async () => {
      const service = build({ project: 'p404' });
      response.error(new HttpErrorResponse({ status: 404, error: { detail: 'Project not found.' } }));
      await Promise.resolve();
      expect(service.projectError()).toBe('Project not found.');
      expect(service.projectPending()).toBe(false);
      expect(await service.resolveBoundAgentId()).toBeNull();
    });

    it('is ignored in a build with Projects off', () => {
      const service = build({ project: 'p1' }, { projectsOn: false });
      expect(service.boundProject()).toBeNull();
      expect(service.boundAgentId()).toBeNull();
      expect(service.projectPending()).toBe(false);
      expect(get).not.toHaveBeenCalled();
    });
  });

  describe('an `?assistantId=` URL', () => {
    it('recognises an older link that names a harness, by the project list', () => {
      const service = build({ assistantId: 'harness-1' });
      expect(service.boundProject()?.projectId).toBe('p1');
      expect(service.boundAgentId()).toBe('harness-1');
    });

    it('is a plain Agent conversation otherwise', () => {
      const service = build({ assistantId: 'agent-7' });
      expect(service.boundProject()).toBeNull();
      expect(service.boundAgentId()).toBe('agent-7');
    });
  });

  describe('queryParamsFor', () => {
    it('names a harness by its project, clearing assistantId', () => {
      const service = build({});
      expect(service.queryParamsFor('harness-1')).toEqual({ project: 'p1', assistantId: null });
    });

    it('names any other Agent as itself, clearing project', () => {
      const service = build({});
      expect(service.queryParamsFor('agent-7')).toEqual({ project: null, assistantId: 'agent-7' });
    });

    it('clears both for an unbound conversation', () => {
      const service = build({});
      expect(service.queryParamsFor(null)).toEqual({ project: null, assistantId: null });
    });

    it('finds a project it was told about, though the list does not hold it', () => {
      const service = build({}, { list: [] });
      expect(service.queryParamsFor('harness-9')).toEqual({ project: null, assistantId: 'harness-9' });
      service.knowProject(project({ projectId: 'p9', harnessAgentId: 'harness-9' }));
      expect(service.queryParamsFor('harness-9')).toEqual({ project: 'p9', assistantId: null });
    });
  });

  describe('bindingQueryParams', () => {
    it('prefers the project when there is one and Projects are on', () => {
      expect(bindingQueryParams('harness-1', 'p1', true)).toEqual({ project: 'p1', assistantId: null });
      expect(bindingQueryParams('harness-1', 'p1', false)).toEqual({ project: null, assistantId: 'harness-1' });
      expect(bindingQueryParams('agent-7', undefined, true)).toEqual({ project: null, assistantId: 'agent-7' });
    });
  });
});
