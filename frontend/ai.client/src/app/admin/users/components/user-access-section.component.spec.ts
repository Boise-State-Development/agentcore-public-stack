/**
 * The Direct access card: three pickers drawn by the shared selector, a full-
 * replace Save, and Remove. What these pin down is the round trip — the loaded
 * grant checks the right rows, a toggle makes the card dirty, Save sends every
 * list, and the empty / expired / unavailable states read correctly.
 */
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { Dialog } from '@angular/cdk/dialog';
import { of, throwError } from 'rxjs';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  UserAccessSectionComponent,
  isoToLocalInput,
  localInputToIso,
} from './user-access-section.component';
import { UserGrantHttpService } from '../services/user-grant-http.service';
import { AdminToolService } from '../../tools/services/admin-tool.service';
import { ManagedModelsService } from '../../manage-models/services/managed-models.service';
import { AdminSkillService } from '../../skills/services/admin-skill.service';
import { ToastService } from '../../../services/toast/toast.service';
import { UserGrant } from '../models';

const TOOLS = [
  { toolId: 'web_search', displayName: 'Web Search', description: 'Search the web', category: 'search', status: 'active' },
  { toolId: 'calculator', displayName: 'Calculator', description: 'Arithmetic', category: 'utility', status: 'active' },
];
const MODELS = [
  { id: 'm1', modelId: 'us.anthropic.claude-haiku-4-5', modelName: 'Claude Haiku 4.5', providerName: 'Anthropic', enabled: true },
  { id: 'm2', modelId: 'amazon.nova-micro-v1:0', modelName: 'Nova Micro', providerName: 'Amazon', enabled: false },
];
const SKILLS = [
  { skillId: 'brand_deck', displayName: 'Brand Deck', description: 'Slides', category: 'docs', status: 'active' },
  { skillId: 'draft_skill', displayName: 'Draft Skill', description: 'WIP', category: null, status: 'draft' },
];

function grant(overrides: Partial<UserGrant> = {}): UserGrant {
  return {
    userId: 'user-1',
    grantedTools: [],
    grantedModels: [],
    grantedSkills: [],
    expiresAt: null,
    note: '',
    grantedBy: null,
    createdAt: '',
    updatedAt: '',
    active: true,
    ...overrides,
  };
}

describe('UserAccessSectionComponent', () => {
  let fixture: ComponentFixture<UserAccessSectionComponent>;
  let el: HTMLElement;
  let http: {
    getGrant: ReturnType<typeof vi.fn>;
    setGrant: ReturnType<typeof vi.fn>;
    deleteGrant: ReturnType<typeof vi.fn>;
  };
  let toast: { success: ReturnType<typeof vi.fn> };
  let dialogResult = true;

  async function mount(loaded: UserGrant | HttpErrorResponse = grant()): Promise<void> {
    http = {
      getGrant: vi.fn(() =>
        loaded instanceof HttpErrorResponse ? throwError(() => loaded) : of(loaded),
      ),
      setGrant: vi.fn((_id: string, update: unknown) => of(grant(update as Partial<UserGrant>))),
      deleteGrant: vi.fn(() => of(undefined)),
    };
    toast = { success: vi.fn() };
    TestBed.configureTestingModule({
      providers: [
        { provide: UserGrantHttpService, useValue: http },
        { provide: ToastService, useValue: toast },
        { provide: Dialog, useValue: { open: () => ({ closed: of(dialogResult) }) } },
        { provide: AdminToolService, useValue: { toolsResource: { isLoading: () => false }, getTools: () => TOOLS } },
        {
          provide: ManagedModelsService,
          useValue: { modelsResource: { isLoading: () => false }, getManagedModels: () => MODELS },
        },
        { provide: AdminSkillService, useValue: { skillsResource: { isLoading: () => false }, getSkills: () => SKILLS } },
      ],
    });
    fixture = TestBed.createComponent(UserAccessSectionComponent);
    fixture.componentRef.setInput('userId', 'user-1');
    el = fixture.nativeElement;
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  afterEach(() => {
    dialogResult = true;
    TestBed.resetTestingModule();
  });

  function picker(headingId: string): HTMLElement {
    const list = el.querySelector<HTMLElement>(`[role="group"][aria-labelledby="${headingId}"]`);
    expect(list, `picker labelled by #${headingId}`).toBeTruthy();
    return list!;
  }

  function checkbox(headingId: string, name: string): HTMLInputElement {
    const list = picker(headingId);
    const label = [...list.querySelectorAll('span[id$="-name"]')].find((s) => s.textContent?.trim() === name);
    expect(label, `row "${name}"`).toBeTruthy();
    return list.querySelector<HTMLInputElement>(`input[aria-labelledby="${label!.id}"]`)!;
  }

  function click(input: HTMLInputElement): void {
    input.click();
    fixture.detectChanges();
  }

  function saveButton(): HTMLButtonElement {
    return [...el.querySelectorAll<HTMLButtonElement>('button[type="submit"]')][0];
  }

  function removeButton(): HTMLButtonElement | undefined {
    return [...el.querySelectorAll<HTMLButtonElement>('button')].find((b) => b.textContent?.trim() === 'Remove grant');
  }

  it('draws tools, models and skills with the shared selector and checks the loaded grant', async () => {
    await mount(grant({ grantedTools: ['web_search'], grantedModels: ['amazon.nova-micro-v1:0'], grantedSkills: ['brand_deck'] }));

    expect(http.getGrant).toHaveBeenCalledWith('user-1');
    expect(el.querySelectorAll('app-tool-selector')).toHaveLength(3);
    expect(checkbox('user-access-tools-heading', 'Web Search').checked).toBe(true);
    expect(checkbox('user-access-tools-heading', 'Calculator').checked).toBe(false);
    expect(checkbox('user-access-models-heading', 'Nova Micro').checked).toBe(true);
    expect(checkbox('user-access-skills-heading', 'Brand Deck').checked).toBe(true);
    expect(checkbox('user-access-skills-heading', 'Draft Skill').checked).toBe(false);
    expect(el.textContent).toContain('Direct grant active');
    expect(removeButton()).toBeTruthy();
  });

  it('reads as "no direct grant" for the empty shape, with nothing to remove and nothing to save', async () => {
    await mount();

    expect(el.textContent).toContain('No direct grant');
    expect(el.textContent).not.toContain('Direct grant active');
    expect(removeButton()).toBeUndefined();
    expect(saveButton().disabled).toBe(true);
  });

  it('a toggle makes the card dirty and Save sends every list as a full replace', async () => {
    await mount(grant({ grantedTools: ['web_search'] }));
    expect(saveButton().disabled).toBe(true);

    click(checkbox('user-access-tools-heading', 'Calculator'));
    click(checkbox('user-access-models-heading', 'Claude Haiku 4.5'));
    expect(saveButton().disabled).toBe(false);

    saveButton().click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(http.setGrant).toHaveBeenCalledWith('user-1', {
      grantedTools: ['calculator', 'web_search'],
      grantedModels: ['us.anthropic.claude-haiku-4-5'],
      grantedSkills: [],
      expiresAt: null,
      note: '',
    });
    expect(toast.success).toHaveBeenCalledWith('Direct access saved');
    // The saved grant is the new baseline: nothing left to save.
    expect(saveButton().disabled).toBe(true);
  });

  it('unticking everything saves an empty grant and says it was removed', async () => {
    await mount(grant({ grantedTools: ['web_search'] }));

    click(checkbox('user-access-tools-heading', 'Web Search'));
    saveButton().click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(http.setGrant.mock.calls[0][1].grantedTools).toEqual([]);
    expect(toast.success).toHaveBeenCalledWith('Direct grant removed');
    expect(el.textContent).toContain('No direct grant');
  });

  it('shows the expiry in the local input and sends it back as ISO', async () => {
    const iso = '2999-01-01T12:00:00+00:00';
    await mount(grant({ grantedTools: ['web_search'], expiresAt: iso }));

    const input = el.querySelector<HTMLInputElement>('#user-access-expires')!;
    expect(input.value).toBe(isoToLocalInput(iso));
    expect(saveButton().disabled).toBe(true);

    fixture.componentInstance.form.controls.note.setValue('pilot');
    fixture.detectChanges();
    saveButton().click();
    await fixture.whenStable();

    const body = http.setGrant.mock.calls[0][1];
    expect(body.note).toBe('pilot');
    expect(new Date(body.expiresAt).getTime()).toBe(new Date(iso).getTime());
  });

  it('marks an expired grant', async () => {
    await mount(grant({ grantedTools: ['web_search'], expiresAt: '2000-01-01T00:00:00+00:00', active: false }));
    expect(el.textContent).toContain('Expired');
  });

  it('Remove grant asks first, then deletes and clears the card', async () => {
    await mount(grant({ grantedTools: ['web_search'], grantedBy: 'admin@example.com' }));

    removeButton()!.click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(http.deleteGrant).toHaveBeenCalledWith('user-1');
    expect(toast.success).toHaveBeenCalledWith('Direct grant removed');
    expect(checkbox('user-access-tools-heading', 'Web Search').checked).toBe(false);
    expect(el.textContent).toContain('No direct grant');
  });

  it('a cancelled confirmation deletes nothing', async () => {
    dialogResult = false;
    await mount(grant({ grantedTools: ['web_search'] }));

    removeButton()!.click();
    await fixture.whenStable();

    expect(http.deleteGrant).not.toHaveBeenCalled();
  });

  it('reads a 404 as "not turned on here" rather than as an error', async () => {
    await mount(new HttpErrorResponse({ status: 404 }));

    expect(el.textContent).toContain('not turned on in this environment');
    expect(el.querySelector('[role="alert"]')).toBeNull();
    expect(el.querySelectorAll('app-tool-selector')).toHaveLength(0);
  });

  it('surfaces the server’s reason when a save is refused', async () => {
    await mount();
    http.setGrant.mockImplementation(() =>
      throwError(() => new HttpErrorResponse({ status: 400, error: { detail: 'Unknown tool id(s): nope.' } })),
    );

    click(checkbox('user-access-tools-heading', 'Calculator'));
    saveButton().click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(el.querySelector('[role="alert"]')?.textContent).toContain('Unknown tool id(s): nope.');
    expect(saveButton().disabled).toBe(false);
  });

  it('tags disabled models and non-active skills without blocking the grant', async () => {
    await mount();
    expect(picker('user-access-models-heading').textContent).toContain('disabled');
    expect(picker('user-access-skills-heading').textContent).toContain('draft');
    expect(checkbox('user-access-skills-heading', 'Draft Skill').disabled).toBe(false);
  });
});

describe('datetime-local conversion', () => {
  it('round-trips through local time', () => {
    const iso = '2999-06-15T08:30:00.000Z';
    const local = isoToLocalInput(iso);
    expect(local).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/);
    expect(new Date(localInputToIso(local)!).getTime()).toBe(new Date(iso).getTime());
  });

  it('treats blank and unparseable as no expiry', () => {
    expect(isoToLocalInput(null)).toBe('');
    expect(isoToLocalInput('never')).toBe('');
    expect(localInputToIso('')).toBeNull();
    expect(localInputToIso('garbage')).toBeNull();
  });
});
