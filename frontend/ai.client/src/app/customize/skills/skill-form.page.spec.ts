import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { HttpErrorResponse } from '@angular/common/http';
import { of } from 'rxjs';
import { ActivatedRoute, Router, convertToParamMap, provideRouter } from '@angular/router';
import { environment } from '../../../environments/environment';
import { FEATURES } from '../../services/features';
import { SkillMdParseResult } from '../../shared/skills/skill-md-parse';
import { MySkillService } from './services/my-skill.service';
import { SkillFormPage } from './skill-form.page';

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

const PARSED: SkillMdParseResult = {
  name: 'pdf-tools',
  displayName: 'Pdf tools',
  description: 'Fill and merge PDFs.',
  instructions: '# PDF tools',
  allowedTools: ['Read'],
  skillMetadata: { license: 'MIT', metadata: { author: 'example-org', version: '1.2' } },
  diagnostics: [
    { level: 'warning', field: '', code: 'frontmatter_repaired', message: 'Repaired <i>colons</i>.' },
    { level: 'error', field: 'name', code: 'name_invalid', message: 'name may use only lowercase.' },
  ],
};

describe('SkillFormPage (My Skills) — Import a SKILL.md', () => {
  let service: {
    parseSkillMd: ReturnType<typeof vi.fn>;
    createSkill: ReturnType<typeof vi.fn>;
    error$: () => string | null;
  };

  beforeEach(() => {
    service = {
      parseSkillMd: vi.fn().mockResolvedValue(PARSED),
      createSkill: vi.fn().mockResolvedValue({ skillId: 'pdf_tools' }),
      error$: () => null,
    };
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: MySkillService, useValue: service },
        { provide: FEATURES, useValue: { ...environment.features, skillBundleImport: false } },
        {
          provide: ActivatedRoute,
          useValue: {
            snapshot: { paramMap: convertToParamMap({}), queryParamMap: convertToParamMap({}) },
          },
        },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  async function chooseFile(): Promise<ComponentFixture<SkillFormPage>> {
    const fixture = TestBed.createComponent(SkillFormPage);
    fixture.detectChanges();
    const input = (fixture.nativeElement as HTMLElement).querySelector<HTMLInputElement>(
      '#skill-md-import',
    )!;
    const file = new File(['---\nname: pdf-tools\n---\n'], 'SKILL.md', { type: 'text/markdown' });
    Object.defineProperty(input, 'files', { value: [file], configurable: true });
    input.dispatchEvent(new Event('change'));
    await settle();
    fixture.detectChanges();
    return fixture;
  }

  it('posts the chosen file to the parse endpoint and patches the form', async () => {
    const fixture = await chooseFile();
    expect(service.parseSkillMd).toHaveBeenCalledTimes(1);
    expect((service.parseSkillMd.mock.calls[0][0] as File).name).toBe('SKILL.md');
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector<HTMLInputElement>('#displayName')!.value).toBe('Pdf tools');
    expect(el.querySelector<HTMLTextAreaElement>('#description')!.value).toBe('Fill and merge PDFs.');
  });

  it('shows the diagnostics as text, errors and warnings distinguished', async () => {
    const fixture = await chooseFile();
    const rows = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll<HTMLLIElement>(
        '[data-testid="skill-md-diagnostics"] li',
      ),
    );
    expect(rows.map((r) => r.dataset['level'])).toEqual(['error', 'warning']);
    expect(rows[0].textContent).toContain('Error:');
    expect(rows[1].textContent).toContain('Warning: Repaired <i>colons</i>.');
    expect(rows[1].querySelector('i')).toBeNull();
  });

  it('saves the imported tools and metadata with the skill', async () => {
    const fixture = await chooseFile();
    vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    (fixture.nativeElement as HTMLElement).querySelector<HTMLFormElement>('form')!.dispatchEvent(
      new Event('submit'),
    );
    await settle();
    expect(service.createSkill).toHaveBeenCalledWith(
      expect.objectContaining({
        displayName: 'Pdf tools',
        allowedTools: ['Read'],
        skillMetadata: PARSED.skillMetadata,
      }),
    );
  });

  it('shows the server’s reason when the file is refused', async () => {
    service.parseSkillMd.mockRejectedValue({ error: { detail: 'SKILL.md is larger than 200 KiB.' } });
    const fixture = await chooseFile();
    expect((fixture.nativeElement as HTMLElement).textContent).toContain(
      'SKILL.md is larger than 200 KiB.',
    );
  });
});

describe('SkillFormPage (My Skills) — reach confirmation on save', () => {
  const reach = 'This skill runs unpinned in 1 shared agent ("Contracts Desk").';
  const conflict = new HttpErrorResponse({
    status: 409,
    error: { detail: { code: 'skill_reach_unconfirmed', message: 'Confirm', reach } },
  });
  let service: {
    getSkill: ReturnType<typeof vi.fn>;
    updateSkill: ReturnType<typeof vi.fn>;
    error$: () => string | null;
  };
  let dialog: { open: ReturnType<typeof vi.fn> };

  function setup(confirmed: boolean) {
    service = {
      getSkill: vi.fn().mockResolvedValue({
        skillId: 'grant_review',
        displayName: 'Grant Review',
        description: 'Reviews grants.',
        instructions: 'Step 1.',
        allowedTools: [],
        skillMetadata: {},
        resources: [],
        status: 'active',
        category: null,
        createdAt: null,
        updatedAt: null,
      }),
      updateSkill: vi.fn().mockRejectedValueOnce(conflict).mockResolvedValue({ skillId: 'grant_review' }),
      error$: () => null,
    };
    dialog = { open: vi.fn(() => ({ closed: of(confirmed) })) };
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: MySkillService, useValue: service },
        { provide: Dialog, useValue: dialog },
        { provide: FEATURES, useValue: { ...environment.features, skillBundleImport: false } },
        {
          provide: ActivatedRoute,
          useValue: {
            snapshot: {
              paramMap: convertToParamMap({ skillId: 'grant_review' }),
              queryParamMap: convertToParamMap({}),
            },
          },
        },
      ],
    });
  }

  afterEach(() => TestBed.resetTestingModule());

  async function submit(): Promise<ReturnType<typeof vi.spyOn>> {
    const fixture = TestBed.createComponent(SkillFormPage);
    fixture.detectChanges();
    await settle();
    fixture.detectChanges();
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    (fixture.nativeElement as HTMLElement).querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit'));
    await settle();
    await settle();
    return navigate;
  }

  it('shows the reach summary and resubmits with confirmReach on confirm', async () => {
    setup(true);
    const navigate = await submit();
    expect(dialog.open).toHaveBeenCalledTimes(1);
    expect(dialog.open.mock.calls[0][1].data.message).toBe(reach);
    expect(service.updateSkill).toHaveBeenCalledTimes(2);
    expect(service.updateSkill.mock.calls[0][1].confirmReach).toBeUndefined();
    expect(service.updateSkill.mock.calls[1][1]).toEqual(expect.objectContaining({ confirmReach: true }));
    expect(navigate).toHaveBeenCalledWith(['/customize/skills']);
  });

  it('saves nothing more and stays on the page when the user keeps editing', async () => {
    setup(false);
    const navigate = await submit();
    expect(service.updateSkill).toHaveBeenCalledTimes(1);
    expect(navigate).not.toHaveBeenCalled();
  });
});
