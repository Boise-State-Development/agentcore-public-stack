import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { AdminSkillService } from './admin-skill.service';
import { ConfigService } from '../../../services/config.service';

describe('AdminSkillService', () => {
  let service: AdminSkillService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        AdminSkillService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(AdminSkillService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  it('should fetch skills', async () => {
    const mockResponse = { skills: [], total: 0 };
    const promise = service.fetchSkills();
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/skills/').flush(mockResponse);
    });
    expect(await promise).toEqual(mockResponse);
  });

  it('should fetch skill by id', async () => {
    const mockSkill = { skillId: 'pdf_workflows', displayName: 'PDF' };
    const promise = service.fetchSkill('pdf_workflows');
    await vi.waitFor(() => {
      httpMock
        .expectOne('http://localhost:8000/admin/skills/pdf_workflows')
        .flush(mockSkill);
    });
    expect(await promise).toEqual(mockSkill);
  });

  it('should create skill', async () => {
    const skillData = { skillId: 'pdf_workflows', displayName: 'PDF', description: 'x' } as any;
    const mockSkill = { skillId: 'pdf_workflows', displayName: 'PDF' };
    const promise = service.createSkill(skillData);
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/admin/skills/').flush(mockSkill);
    });
    expect(await promise).toEqual(mockSkill);
  });

  it('should update skill', async () => {
    const updates = { displayName: 'Updated' } as any;
    const mockSkill = { skillId: 'pdf_workflows', displayName: 'Updated' };
    const promise = service.updateSkill('pdf_workflows', updates);
    await vi.waitFor(() => {
      httpMock
        .expectOne('http://localhost:8000/admin/skills/pdf_workflows')
        .flush(mockSkill);
    });
    expect(await promise).toEqual(mockSkill);
  });

  it('should delete skill', async () => {
    const promise = service.deleteSkill('pdf_workflows');
    await vi.waitFor(() => {
      httpMock
        .expectOne('http://localhost:8000/admin/skills/pdf_workflows?hard=false')
        .flush(null);
    });
    await promise;
  });

  it('should get skill roles', async () => {
    const promise = service.getSkillRoles('pdf_workflows');
    await vi.waitFor(() => {
      httpMock
        .expectOne('http://localhost:8000/admin/skills/pdf_workflows/roles')
        .flush({ skillId: 'pdf_workflows', roles: [{ roleId: 'editor' }] });
    });
    expect(await promise).toEqual([{ roleId: 'editor' }]);
  });

  it('should list resources', async () => {
    const promise = service.listResources('pdf_workflows');
    await vi.waitFor(() => {
      httpMock
        .expectOne('http://localhost:8000/admin/skills/pdf_workflows/resources')
        .flush({ skillId: 'pdf_workflows', resources: [{ filename: 'forms.md' }] });
    });
    expect(await promise).toEqual([{ filename: 'forms.md' }]);
  });

  it('should upload a resource as multipart and return the manifest', async () => {
    const file = new File(['# Forms'], 'forms.md', { type: 'text/markdown' });
    const promise = service.uploadResource('pdf_workflows', file);
    await vi.waitFor(() => {
      const req = httpMock.expectOne(
        'http://localhost:8000/admin/skills/pdf_workflows/resources'
      );
      expect(req.request.body instanceof FormData).toBe(true);
      req.flush({ skillId: 'pdf_workflows', resources: [{ filename: 'forms.md' }] });
    });
    expect(await promise).toEqual([{ filename: 'forms.md' }]);
  });

  it('should read a resource as text', async () => {
    const promise = service.readResource('pdf_workflows', 'forms.md');
    await vi.waitFor(() => {
      httpMock
        .expectOne('http://localhost:8000/admin/skills/pdf_workflows/resources/forms.md')
        .flush('# Forms body');
    });
    expect(await promise).toBe('# Forms body');
  });

  it('should delete a resource and return the manifest', async () => {
    const promise = service.deleteResource('pdf_workflows', 'forms.md');
    await vi.waitFor(() => {
      httpMock
        .expectOne('http://localhost:8000/admin/skills/pdf_workflows/resources/forms.md')
        .flush({ skillId: 'pdf_workflows', resources: [] });
    });
    expect(await promise).toEqual([]);
  });
});
