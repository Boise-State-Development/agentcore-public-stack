import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { ActivatedRoute, Router, convertToParamMap, provideRouter } from '@angular/router';
import { signal } from '@angular/core';
import { SkillFormPage } from './skill-form.page';
import { ConfigService } from '../../../services/config.service';

const API = 'http://localhost:8000';
const settle = () => new Promise(resolve => setTimeout(resolve, 0));

const CATALOG_SKILL = {
  skillId: 'pdf_workflows',
  displayName: 'PDF Workflows',
  description: 'Fill, merge and split PDFs.',
  instructions: '# PDF Workflows',
  compose: [],
  allowedTools: [],
  skillMetadata: {},
  resources: [],
  status: 'active',
  category: null,
  allowedAppRoles: [],
  createdAt: null,
  updatedAt: null,
};

describe('SkillFormPage (admin) — Download skill', () => {
  let http: HttpTestingController;

  function configure(skillId: string | null): void {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: ConfigService, useValue: { appApiUrl: signal(API) } },
        {
          provide: ActivatedRoute,
          useValue: { snapshot: { paramMap: convertToParamMap(skillId ? { skillId } : {}) } },
        },
      ],
    });
    http = TestBed.inject(HttpTestingController);
  }

  afterEach(() => {
    http.match(() => true);
    TestBed.resetTestingModule();
  });

  async function create(load?: { status: number; body?: object }): Promise<ComponentFixture<SkillFormPage>> {
    const fixture = TestBed.createComponent(SkillFormPage);
    fixture.detectChanges();
    await settle();
    if (load) {
      const req = http.expectOne(`${API}/admin/skills/pdf_workflows`);
      if (load.status === 200) req.flush(load.body ?? CATALOG_SKILL);
      else req.flush({ detail: 'not found' }, { status: load.status, statusText: 'Not Found' });
    }
    // The service's catalog resource lists skills as soon as it is injected.
    http.match(`${API}/admin/skills/`).forEach(req => req.flush({ skills: [], total: 0 }));
    await fixture.whenStable();
    await settle();
    fixture.detectChanges();
    return fixture;
  }

  const downloadButton = (fixture: ComponentFixture<SkillFormPage>) =>
    Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll<HTMLButtonElement>('button'),
    ).find(b => b.textContent?.includes('Download skill'));

  it('offers Download skill once a catalog skill has loaded', async () => {
    configure('pdf_workflows');
    const fixture = await create({ status: 200 });
    expect(downloadButton(fixture)).toBeTruthy();
  });

  it('offers no download when the skill is not a catalog skill (404)', async () => {
    configure('pdf_workflows');
    const fixture = await create({ status: 404 });
    expect(downloadButton(fixture)).toBeUndefined();
  });

  it('offers no download on the create form', async () => {
    configure(null);
    const fixture = await create();
    expect(downloadButton(fixture)).toBeUndefined();
  });

  it('downloads through the admin export route', async () => {
    configure('pdf_workflows');
    const fixture = await create({ status: 200 });
    const original = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
    URL.createObjectURL = vi.fn(() => 'blob:skill');
    URL.revokeObjectURL = vi.fn();
    const saved: string[] = [];
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function (this: HTMLAnchorElement) {
        saved.push(this.download);
      });
    try {
      downloadButton(fixture)!.click();
      await settle();
      const req = http.expectOne(`${API}/admin/skills/pdf_workflows/export`);
      expect(req.request.method).toBe('GET');
      expect(req.request.responseType).toBe('blob');
      req.flush(new Blob(['zip'], { type: 'application/zip' }));
      await fixture.whenStable();
      await settle();
      expect(saved).toEqual(['pdf-workflows.zip']);
    } finally {
      click.mockRestore();
      URL.createObjectURL = original.create;
      URL.revokeObjectURL = original.revoke;
    }
  });
});

describe('SkillFormPage (admin) — Import SKILL.md', () => {
  let http: HttpTestingController;

  const PARSED = {
    name: 'pdf-tools',
    displayName: 'Pdf tools',
    description: 'Fill and merge PDFs.',
    instructions: '# PDF tools',
    allowedTools: ['Read', 'Bash(python:*)'],
    skillMetadata: {
      license: 'Apache-2.0',
      compatibility: 'Needs uv.',
      metadata: { author: 'example-org', version: '1.2' },
    },
    diagnostics: [
      { level: 'warning', field: 'x-custom', code: 'unknown_field', message: 'x-custom is not standard.' },
      { level: 'error', field: 'description', code: 'unsafe_character', message: '<b>U+202E</b> found.' },
    ],
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: ConfigService, useValue: { appApiUrl: signal(API) } },
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({}) } } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.match(() => true);
    TestBed.resetTestingModule();
  });

  async function render(): Promise<ComponentFixture<SkillFormPage>> {
    const fixture = TestBed.createComponent(SkillFormPage);
    fixture.detectChanges();
    await settle();
    http.match(`${API}/admin/skills/`).forEach(req => req.flush({ skills: [], total: 0 }));
    await fixture.whenStable();
    return fixture;
  }

  async function choose(fixture: ComponentFixture<SkillFormPage>, body: object, status = 200) {
    const input = (fixture.nativeElement as HTMLElement).querySelector<HTMLInputElement>('#skillImport')!;
    const file = new File(['---\nname: pdf-tools\n---\n'], 'SKILL.md', { type: 'text/markdown' });
    Object.defineProperty(input, 'files', { value: [file], configurable: true });
    input.dispatchEvent(new Event('change'));
    await settle();
    const req = http.expectOne(`${API}/admin/skills/import/parse`);
    expect(req.request.method).toBe('POST');
    const form = req.request.body as FormData;
    expect((form.get('file') as File).name).toBe('SKILL.md');
    if (status === 200) req.flush(body);
    else req.flush(body, { status, statusText: 'Bad Request' });
    await settle();
    fixture.detectChanges();
    return req;
  }

  it('posts the file to the admin parse route and patches the form', async () => {
    const fixture = await render();
    await choose(fixture, PARSED);
    const page = fixture.componentInstance;
    expect(page.form.getRawValue()).toMatchObject({
      skillId: 'pdf_tools',
      displayName: 'Pdf tools',
      description: 'Fill and merge PDFs.',
      instructions: '# PDF tools',
    });
  });

  it('lists the diagnostics as text, errors first, labelled by word', async () => {
    const fixture = await render();
    await choose(fixture, PARSED);
    const rows = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll<HTMLLIElement>(
        '[data-testid="skill-md-diagnostics"] li',
      ),
    );
    expect(rows.map(r => r.dataset['level'])).toEqual(['error', 'warning']);
    expect(rows[0].textContent).toContain('Error:');
    expect(rows[1].textContent).toContain('Warning:');
    // Third-party text is never parsed as markup.
    expect(rows[0].textContent).toContain('<b>U+202E</b> found.');
    expect(rows[0].querySelector('b')).toBeNull();
  });

  it('keeps allowedTools and skillMetadata and sends them on create', async () => {
    const fixture = await render();
    await choose(fixture, PARSED);
    const page = fixture.componentInstance;
    vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    const saving = page.onSubmit();
    await settle();
    const req = http.expectOne(r => r.method === 'POST' && r.url === `${API}/admin/skills/`);
    expect(req.request.body).toMatchObject({
      skillId: 'pdf_tools',
      allowedTools: ['Read', 'Bash(python:*)'],
      skillMetadata: PARSED.skillMetadata,
    });
    req.flush({ ...CATALOG_SKILL, skillId: 'pdf_tools' });
    await saving;
  });

  it('shows the server’s reason when the file cannot be read', async () => {
    const fixture = await render();
    await choose(fixture, { detail: 'SKILL.md is not UTF-8 text.' }, 400);
    expect((fixture.nativeElement as HTMLElement).textContent).toContain('SKILL.md is not UTF-8 text.');
  });
});
