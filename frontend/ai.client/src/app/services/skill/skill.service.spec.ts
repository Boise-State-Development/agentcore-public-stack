import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { SkillService, UserSkill, SkillsResponse } from './skill.service';
import { ConfigService } from '../config.service';
import { ChatModeService } from '../chat-mode/chat-mode.service';
import { signal } from '@angular/core';

describe('SkillService', () => {
  let service: SkillService;
  let httpMock: HttpTestingController;

  const mockSkills: UserSkill[] = [
    { skillId: 'pdf_workflows', displayName: 'PDF Workflows', description: 'Work with PDFs', category: 'document', boundToolCount: 2, userEnabled: null, isEnabled: true },
    { skillId: 'web_research', displayName: 'Web Research', description: 'Research the web', category: 'research', boundToolCount: 3, userEnabled: false, isEnabled: false },
  ];

  const mockResponse: SkillsResponse = { skills: mockSkills, totalCount: 2 };

  function configure(skillsEnabled: boolean) {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        SkillService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
        { provide: ChatModeService, useValue: { skillsEnabled: signal(skillsEnabled) } },
      ],
    });

    service = TestBed.inject(SkillService);
    httpMock = TestBed.inject(HttpTestingController);
    // Flush the constructor effect that gates the auto-load on the policy.
    TestBed.tick();
  }

  async function setup() {
    configure(true);
    await vi.waitFor(() => {
      httpMock.expectOne('http://localhost:8000/skills/').flush(mockResponse);
    });
  }

  afterEach(() => {
    TestBed.resetTestingModule();
    httpMock.match(() => true);
  });

  describe('auto-load gating', () => {
    it('does not fetch skills when the feature is disabled', () => {
      configure(false);
      // No /skills/ request is issued; verify() in afterEach would fail on one.
      httpMock.verify();
      expect(service.initialized()).toBe(false);
      expect(service.skills()).toEqual([]);
    });
  });

  describe('loadSkills', () => {
    beforeEach(setup);

    it('should load skills from constructor', () => {
      expect(service.skills()).toEqual(mockSkills);
      expect(service.initialized()).toBe(true);
      expect(service.loading()).toBe(false);
    });

    it('should handle error', async () => {
      const promise = service.loadSkills();
      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/skills/').error(new ProgressEvent('error'));
      });
      await promise;
      expect(service.error()).toBeTruthy();
    });
  });

  describe('computed signals', () => {
    beforeEach(setup);

    it('should compute enabled skill ids from effective state', () => {
      expect(service.enabledSkillIds()).toEqual(['pdf_workflows']);
      expect(service.enabledCount()).toBe(1);
      expect(service.hasSkills()).toBe(true);
    });
  });

  describe('toggleSkill', () => {
    beforeEach(setup);

    it('should optimistically update and persist the preference', async () => {
      const promise = service.toggleSkill('pdf_workflows');
      expect(service.getSkill('pdf_workflows')?.isEnabled).toBe(false);

      await vi.waitFor(() => {
        const req = httpMock.expectOne('http://localhost:8000/skills/preferences');
        expect(req.request.method).toBe('PUT');
        expect(req.request.body).toEqual({ preferences: { pdf_workflows: false } });
        req.flush({});
      });
      await promise;
      expect(service.getSkill('pdf_workflows')?.userEnabled).toBe(false);
    });

    it('should revert on error', async () => {
      const promise = service.toggleSkill('pdf_workflows');
      await vi.waitFor(() => {
        httpMock.expectOne('http://localhost:8000/skills/preferences').error(new ProgressEvent('error'));
      });
      await expect(promise).rejects.toThrow();
      expect(service.getSkill('pdf_workflows')?.isEnabled).toBe(true);
      expect(service.getSkill('pdf_workflows')?.userEnabled).toBe(null);
    });
  });
});
