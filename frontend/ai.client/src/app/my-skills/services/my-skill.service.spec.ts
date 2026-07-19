import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { ConfigService } from '../../services/config.service';
import { MySkill } from '../models/my-skill.model';
import { MySkillService } from './my-skill.service';

const BASE = 'http://localhost:8000/skills/mine';

function stubSkill(overrides: Partial<MySkill> = {}): MySkill {
  return {
    skillId: 'grant_writing',
    displayName: 'Grant Writing',
    description: 'How we write grant narratives.',
    instructions: '# Grant Writing',
    allowedTools: [],
    skillMetadata: {},
    resources: [],
    status: 'active',
    category: null,
    createdAt: null,
    updatedAt: null,
    ...overrides,
  };
}

describe('MySkillService', () => {
  let service: MySkillService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
        MySkillService,
      ],
    });
    service = TestBed.inject(MySkillService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  it('loads skills and marks the feature accessible', async () => {
    const promise = service.loadSkills();

    const req = httpMock.expectOne(BASE);
    expect(req.request.method).toBe('GET');
    req.flush({ skills: [stubSkill()], totalCount: 1 });
    await promise;

    expect(service.skills$()).toHaveLength(1);
    expect(service.accessible$()).toBe(true);
    expect(service.error$()).toBeNull();
  });

  it('treats a 404 as the kill switch, not an error', async () => {
    const promise = service.loadSkills();

    httpMock.expectOne(BASE).flush('Not found', { status: 404, statusText: 'Not Found' });
    await promise;

    expect(service.accessible$()).toBe(false);
    expect(service.skills$()).toEqual([]);
    expect(service.error$()).toBeNull();
  });

  it('surfaces a non-404 failure as an error and rethrows', async () => {
    const promise = service.loadSkills();

    httpMock.expectOne(BASE).flush('Boom', { status: 500, statusText: 'Server Error' });

    await expect(promise).rejects.toBeDefined();
    expect(service.error$()).toBeTruthy();
    expect(service.accessible$()).toBeNull();
  });

  it('prefers the backend detail message over a generic HTTP error', async () => {
    const promise = service.createSkill({ displayName: 'X', description: 'd' });

    httpMock
      .expectOne(BASE)
      .flush(
        { detail: 'You already have the maximum of 50 skills.' },
        { status: 409, statusText: 'Conflict' },
      );

    await expect(promise).rejects.toBeDefined();
    expect(service.error$()).toBe('You already have the maximum of 50 skills.');
  });

  it('appends a created skill to local state', async () => {
    const promise = service.createSkill({ displayName: 'Grant Writing', description: 'd' });

    const req = httpMock.expectOne(BASE);
    expect(req.request.method).toBe('POST');
    // No skillId is sent — the backend allocates it.
    expect(req.request.body.skillId).toBeUndefined();
    req.flush(stubSkill());
    await promise;

    expect(service.skills$().map((s) => s.skillId)).toEqual(['grant_writing']);
  });

  it('replaces the updated skill in local state', async () => {
    const load = service.loadSkills();
    httpMock.expectOne(BASE).flush({ skills: [stubSkill()], totalCount: 1 });
    await load;

    const promise = service.updateSkill('grant_writing', { description: 'New.' });
    const req = httpMock.expectOne(`${BASE}/grant_writing`);
    expect(req.request.method).toBe('PUT');
    req.flush(stubSkill({ description: 'New.' }));
    await promise;

    expect(service.skills$()[0].description).toBe('New.');
  });

  it('drops a deleted skill from local state', async () => {
    const load = service.loadSkills();
    httpMock.expectOne(BASE).flush({ skills: [stubSkill()], totalCount: 1 });
    await load;

    const promise = service.deleteSkill('grant_writing');
    httpMock.expectOne(`${BASE}/grant_writing`).flush({ message: 'ok' });
    await promise;

    expect(service.skills$()).toEqual([]);
  });

  it('posts an upload as multipart with the resource kind', async () => {
    const file = new File(['print(1)'], 'build.py', { type: 'text/x-python' });
    const promise = service.uploadResource('grant_writing', file, 'script');

    const req = httpMock.expectOne(`${BASE}/grant_writing/resources`);
    expect(req.request.method).toBe('POST');
    const body = req.request.body as FormData;
    expect(body.get('kind')).toBe('script');
    expect((body.get('file') as File).name).toBe('build.py');

    req.flush({
      skillId: 'grant_writing',
      resources: [
        {
          filename: 'build.py',
          contentHash: 'abc',
          size: 8,
          contentType: 'text/x-python',
          s3Key: 'skills/grant_writing/scripts/build.py',
          kind: 'script',
        },
      ],
    });

    expect((await promise)[0].kind).toBe('script');
  });

  it('encodes the filename when reading and deleting a resource', async () => {
    void service.readResource('grant_writing', 'my notes.md');
    httpMock.expectOne(`${BASE}/grant_writing/resources/my%20notes.md`).flush('body');

    void service.deleteResource('grant_writing', 'my notes.md');
    const del = httpMock.expectOne(`${BASE}/grant_writing/resources/my%20notes.md`);
    expect(del.request.method).toBe('DELETE');
    del.flush({ skillId: 'grant_writing', resources: [] });
  });
});
