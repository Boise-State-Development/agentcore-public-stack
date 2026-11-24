import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentService } from '../../../agents/services/agent.service';
import { ConfigService } from '../../../services/config.service';
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
  let invalidateBindable: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    invalidateBindable = vi.fn();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
        { provide: AgentService, useValue: { invalidateBindable } },
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

  describe('version history', () => {
    it('lists a page with the cursor and reads one version', async () => {
      const list = service.listVersions('grant writing', { limit: 20, before: 7 });
      const req = httpMock.expectOne((r) => r.url === `${BASE}/grant%20writing/versions`);
      expect(req.request.params.get('limit')).toBe('20');
      expect(req.request.params.get('before')).toBe('7');
      expect(req.request.withCredentials).toBe(true);
      req.flush({ versions: [] });
      expect(await list).toEqual({ versions: [] });

      const one = service.getVersion('grant_writing', 3);
      httpMock.expectOne(`${BASE}/grant_writing/versions/3`).flush({ version: 3 });
      expect((await one).version).toBe(3);
    });

    it('restores with the stale check and reach flag, then updates its list', async () => {
      const promise = service.restoreVersion('grant_writing', 2, {
        expectedUpdatedAt: '2026-11-23T10:00:00+00:00',
        confirmReach: true,
      });
      const req = httpMock.expectOne(`${BASE}/grant_writing/versions/2/restore`);
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual({
        expectedUpdatedAt: '2026-11-23T10:00:00+00:00',
        confirmReach: true,
      });
      req.flush({ skill: stubSkill(), version: null, importSourceRestored: true });
      await promise;
      expect(invalidateBindable).toHaveBeenCalledWith('skill');
    });
  });

  describe('bundle import', () => {
    it('previews a GitHub URL as multipart, with the target skill and no path unless chosen', async () => {
      const promise = service.previewImport({ url: ' https://github.com/a/b ' }, 'grant_writing');
      const req = httpMock.expectOne(`${BASE}/import/preview`);
      expect(req.request.method).toBe('POST');
      const body = req.request.body as FormData;
      expect(body.get('url')).toBe('https://github.com/a/b');
      expect(body.get('skillId')).toBe('grant_writing');
      expect(body.has('path')).toBe(false);
      expect(body.has('file')).toBe(false);
      req.flush({ candidates: [], files: [] });
      await promise;
    });

    it("sends a chosen path even when it is the archive's top level", async () => {
      const zip = new File(['PK'], 'bundle.zip', { type: 'application/zip' });
      const promise = service.previewImport({ file: zip, path: '' });
      const req = httpMock.expectOne(`${BASE}/import/preview`);
      const body = req.request.body as FormData;
      expect((body.get('file') as File).name).toBe('bundle.zip');
      expect(body.get('path')).toBe('');
      expect(body.has('url')).toBe(false);
      req.flush({ candidates: [], files: [] });
      await promise;
    });

    it('creates from a bundle with the preview hash and adds the skill to local state', async () => {
      const promise = service.importSkill(
        { url: 'https://github.com/a/b', path: 'skills/x' },
        { displayName: 'X', expectedBundleHash: 'h1' },
      );
      const req = httpMock.expectOne(`${BASE}/import`);
      const body = req.request.body as FormData;
      expect(body.get('path')).toBe('skills/x');
      expect(body.get('displayName')).toBe('X');
      expect(body.get('expectedBundleHash')).toBe('h1');
      req.flush({ skill: stubSkill(), files: [] });
      await promise;
      expect(service.skills$().map((s) => s.skillId)).toEqual(['grant_writing']);
      expect(invalidateBindable).toHaveBeenCalledWith('skill');
    });

    it('re-imports into the skill with the preview hash and updatedAt', async () => {
      const promise = service.reimportSkill(
        'grant_writing',
        { url: 'https://github.com/a/b' },
        { expectedBundleHash: 'h1', expectedUpdatedAt: '2026-11-18T00:00:00+00:00' },
      );
      const req = httpMock.expectOne(`${BASE}/grant_writing/import`);
      const body = req.request.body as FormData;
      expect(body.get('expectedBundleHash')).toBe('h1');
      expect(body.get('expectedUpdatedAt')).toBe('2026-11-18T00:00:00+00:00');
      req.flush({ skill: stubSkill(), files: [] });
      await promise;
      expect(invalidateBindable).toHaveBeenCalledWith('skill');
    });
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

  it('hands a delete reach 409 back to the caller without the error banner', async () => {
    // A project or shared agent uses the skill: the page asks with the server's
    // summary, so a banner saying "Failed to delete" would be wrong.
    const first = service.deleteSkill('grant_writing');
    httpMock.expectOne(`${BASE}/grant_writing`).flush(
      { detail: { code: 'skill_reach_unconfirmed', message: 'm', reach: 'Used by 1 project ("Legal").' } },
      { status: 409, statusText: 'Conflict' },
    );
    await expect(first).rejects.toBeDefined();
    expect(service.error$()).toBeNull();

    const confirmed = service.deleteSkill('grant_writing', { confirmReach: true });
    const req = httpMock.expectOne((r) => r.url === `${BASE}/grant_writing`);
    expect(req.request.method).toBe('DELETE');
    expect(req.request.params.get('confirmReach')).toBe('true');
    req.flush({ message: 'ok' });
    await confirmed;
  });

  it('drops the bindable skill palette after every write, not after a failed one', async () => {
    // A project's Skills page memoises the palette for the whole tab; an authored
    // skill is part of the accessible set, so each write must make it re-fetch.
    const created = service.createSkill({ displayName: 'Grant Writing', description: 'd' });
    httpMock.expectOne(BASE).flush(stubSkill());
    await created;
    expect(invalidateBindable).toHaveBeenCalledWith('skill');

    const updated = service.updateSkill('grant_writing', { description: 'e' });
    httpMock.expectOne(`${BASE}/grant_writing`).flush(stubSkill({ description: 'e' }));
    await updated;

    const deleted = service.deleteSkill('grant_writing');
    httpMock.expectOne(`${BASE}/grant_writing`).flush(null);
    await deleted;
    expect(invalidateBindable).toHaveBeenCalledTimes(3);

    const failed = service.createSkill({ displayName: 'X', description: 'd' });
    httpMock.expectOne(BASE).flush({ detail: 'nope' }, { status: 400, statusText: 'Bad Request' });
    await expect(failed).rejects.toBeDefined();
    expect(invalidateBindable).toHaveBeenCalledTimes(3); // nothing changed server-side
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

  it('keeps the slashes of a nested file path and encodes each segment', async () => {
    void service.readResource('grant_writing', 'legal/v1 notes/nda.md');
    httpMock.expectOne(`${BASE}/grant_writing/resources/legal/v1%20notes/nda.md`).flush('body');

    void service.deleteResource('grant_writing', 'legal/nda.md');
    const del = httpMock.expectOne(`${BASE}/grant_writing/resources/legal/nda.md`);
    expect(del.request.method).toBe('DELETE');
    del.flush({ skillId: 'grant_writing', resources: [] });
  });
});
