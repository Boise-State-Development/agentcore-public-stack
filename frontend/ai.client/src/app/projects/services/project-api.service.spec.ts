import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { Observable } from 'rxjs';
import { ProjectApiService } from './project-api.service';
import { SUPPRESS_ERROR_TOAST } from '../../auth/error.interceptor';
import { ConfigService } from '../../services/config.service';

const BASE = 'http://localhost:8000/projects';

describe('ProjectApiService', () => {
  let service: ProjectApiService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        ProjectApiService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(ProjectApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  /**
   * The kill switch 404s the whole surface on purpose and the pages turn that into a
   * "not available" state, so no call may reach the global error toast. Asserted per
   * call: a deep link to a project tab never calls `list`.
   */
  const calls: Array<[string, string, string, () => Observable<unknown>]> = [
    ['list', 'GET', `${BASE}?includeArchived=true`, () => service.list(true)],
    ['create', 'POST', BASE, () => service.create({ name: 'x' })],
    ['get', 'GET', `${BASE}/prj_1`, () => service.get('prj_1')],
    ['update', 'PATCH', `${BASE}/prj_1`, () => service.update('prj_1', { name: 'y' })],
    ['delete', 'DELETE', `${BASE}/prj_1`, () => service.delete('prj_1')],
    ['transfer', 'POST', `${BASE}/prj_1/transfer`, () => service.transfer('prj_1', 'a@x.edu')],
    ['members', 'GET', `${BASE}/prj_1/members`, () => service.members('prj_1')],
    ['addMembers', 'POST', `${BASE}/prj_1/members`, () => service.addMembers('prj_1', ['a@x.edu'], 'viewer')],
    ['updateMember', 'PATCH', `${BASE}/prj_1/members/a%40x.edu`, () => service.updateMember('prj_1', 'a@x.edu', 'editor')],
    ['removeMember', 'DELETE', `${BASE}/prj_1/members/a%40x.edu`, () => service.removeMember('prj_1', 'a@x.edu')],
    ['leave', 'DELETE', `${BASE}/prj_1/members/me`, () => service.leave('prj_1')],
    ['directory', 'GET', `${BASE}/prj_1/directory?q=ann&limit=10`, () => service.directory('prj_1', 'ann')],
    ['instructions', 'GET', `${BASE}/prj_1/instructions`, () => service.instructions('prj_1')],
    ['saveInstructions', 'PUT', `${BASE}/prj_1/instructions`, () => service.saveInstructions('prj_1', 'Be brief.')],
    ['model', 'GET', `${BASE}/prj_1/model`, () => service.model('prj_1')],
    ['saveModel', 'PUT', `${BASE}/prj_1/model`, () => service.saveModel('prj_1', { modelId: 'm' })],
    ['tools', 'GET', `${BASE}/prj_1/tools`, () => service.bindings('prj_1', 'tools')],
    ['saveSkills', 'PUT', `${BASE}/prj_1/skills`, () => service.saveBindings('prj_1', 'skills', [{ ref: 's' }])],
    ['versions', 'GET', `${BASE}/prj_1/instructions/versions?limit=50`, () => service.versions('prj_1')],
    ['version', 'GET', `${BASE}/prj_1/instructions/versions/3`, () => service.version('prj_1', 3)],
    ['tasks', 'GET', `${BASE}/prj_1/tasks?limit=20`, () => service.tasks('prj_1')],
    ['tasks (page 2)', 'GET', `${BASE}/prj_1/tasks?limit=20&nextToken=abc`, () => service.tasks('prj_1', 20, 'abc')],
    ['sharedTasks', 'GET', `${BASE}/prj_1/shared-tasks`, () => service.sharedTasks('prj_1')],
    ['files', 'GET', `${BASE}/prj_1/knowledge?limit=100`, () => service.files('prj_1')],
    ['file', 'GET', `${BASE}/prj_1/knowledge/doc_1`, () => service.file('prj_1', 'doc_1')],
    ['fileDownloadUrl', 'GET', `${BASE}/prj_1/knowledge/doc_1/download`, () => service.fileDownloadUrl('prj_1', 'doc_1')],
    ['fileUploadUrl', 'POST', `${BASE}/prj_1/knowledge/upload-url`, () => service.fileUploadUrl('prj_1', { filename: 'a.pdf', contentType: 'application/pdf', sizeBytes: 3 })],
    ['reportFileUploadFailure', 'POST', `${BASE}/prj_1/knowledge/doc_1/upload-failed`, () => service.reportFileUploadFailure('prj_1', 'doc_1', 'nope')],
    ['audit', 'GET', `${BASE}/prj_1/audit?limit=50`, () => service.audit('prj_1')],
    ['audit (page 2)', 'GET', `${BASE}/prj_1/audit?limit=50&cursor=c1`, () => service.audit('prj_1', 50, 'c1')],
    ['deleteFile', 'DELETE', `${BASE}/prj_1/knowledge/doc_1`, () => service.deleteFile('prj_1', 'doc_1')],
  ];

  for (const [name, method, url, call] of calls) {
    it(`${name}: ${method} ${url.replace(BASE, '/projects')} without the error toast`, () => {
      call().subscribe();
      const req = httpMock.expectOne(url);
      expect(req.request.method).toBe(method);
      expect(req.request.context.get(SUPPRESS_ERROR_TOAST)).toBe(true);
      req.flush({});
    });
  }

  it('sends the wire shapes the backend reads', () => {
    service.addMembers('prj_1', ['a@x.edu', 'b@x.edu'], 'editor').subscribe();
    expect(httpMock.expectOne(`${BASE}/prj_1/members`).request.body).toEqual({ emails: ['a@x.edu', 'b@x.edu'], role: 'editor' });

    service.saveModel('prj_1', { modelId: 'm', provider: 'bedrock' }).subscribe();
    expect(httpMock.expectOne(`${BASE}/prj_1/model`).request.body).toEqual({ modelConfig: { modelId: 'm', provider: 'bedrock' } });

    service.saveBindings('prj_1', 'tools', [{ ref: 'web_search' }]).subscribe();
    expect(httpMock.expectOne(`${BASE}/prj_1/tools`).request.body).toEqual({ bindings: [{ ref: 'web_search' }] });
  });
});
