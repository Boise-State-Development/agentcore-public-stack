import { Dialog } from '@angular/cdk/dialog';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { FieldSetEditorPage } from './field-set-editor.page';
import { FieldSet, FieldSetStructuredWrite, FieldSetWrite } from './models/field-set.model';
import { FieldSetReadResult, FieldSetService } from './services/field-set.service';

function stubSet(overrides: Partial<FieldSet> = {}): FieldSet {
  return {
    skillId: 'grants',
    name: 'intake',
    filename: 'field-set.intake.md',
    title: 'Grant Intake',
    version: '2',
    fields: [
      {
        name: 'deadline',
        title: 'Deadline',
        type: 'date',
        find: 'The full proposal deadline',
        enum: [],
        optional: false,
        unmanagedValues: ['ask Dana'],
      },
      {
        name: 'cost_share',
        title: 'Cost sharing',
        type: 'enum',
        find: 'Whether cost sharing is required',
        enum: ['required', 'not required'],
        optional: true,
        unmanagedValues: [''],
      },
    ],
    rules: '- deadline must be in the future',
    preamble: 'Read by the grants office.',
    postscript: null,
    frontmatter: ['owner: Research Office'],
    unmanagedColumns: ['note'],
    markdown: '---\ntitle: Grant Intake\n---\n',
    maxFields: 14,
    ...overrides,
  };
}

interface ServiceStub {
  list: ReturnType<typeof vi.fn>;
  read: ReturnType<typeof vi.fn>;
  write: ReturnType<typeof vi.fn>;
  remove: ReturnType<typeof vi.fn>;
  messageFor: (err: unknown, fallback: string) => string;
}

describe('FieldSetEditorPage', () => {
  let fixture: ComponentFixture<FieldSetEditorPage>;
  let service: ServiceStub;
  let el: HTMLElement;
  let navigate: ReturnType<typeof vi.spyOn>;

  async function open(name: string | undefined, read?: FieldSetReadResult): Promise<void> {
    if (read) {
      service.read.mockResolvedValue(read);
    }
    fixture = TestBed.createComponent(FieldSetEditorPage);
    fixture.componentRef.setInput('skillId', 'grants');
    if (name) {
      fixture.componentRef.setInput('name', name);
    }
    el = fixture.nativeElement as HTMLElement;
    fixture.detectChanges();
    await settle();
  }

  /** Let every awaited stub resolve (a load may await twice), then render. */
  async function settle(): Promise<void> {
    for (let i = 0; i < 3; i++) {
      await new Promise((resolve) => setTimeout(resolve));
      await fixture.whenStable();
      fixture.detectChanges();
    }
  }

  function page(): {
    save: () => Promise<void>;
    showMarkdown: () => Promise<void>;
    showForm: () => Promise<void>;
    moveField: (i: number, d: -1 | 1) => void;
    addField: () => void;
    deleteSet: () => Promise<void>;
  } {
    return fixture.componentInstance as unknown as ReturnType<typeof page>;
  }

  function lastBody(): FieldSetWrite {
    return service.write.mock.calls.at(-1)![2] as FieldSetWrite;
  }

  function input(id: string): HTMLInputElement {
    return el.querySelector(`#${id}`) as HTMLInputElement;
  }

  function type(id: string, value: string): void {
    const target = input(id);
    target.value = value;
    target.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  beforeEach(() => {
    service = {
      list: vi.fn().mockResolvedValue({ skillId: 'grants', fieldSets: [], maxFields: 14 }),
      read: vi.fn(),
      write: vi.fn(async (_skill: string, name: string) => stubSet({ name })),
      remove: vi.fn().mockResolvedValue(undefined),
      messageFor: (err: unknown, fallback: string) =>
        ((err as { error?: { detail?: string } })?.error?.detail ?? fallback),
    };
    TestBed.configureTestingModule({
      imports: [FieldSetEditorPage],
      providers: [
        provideRouter([]),
        { provide: FieldSetService, useValue: service },
        { provide: Dialog, useValue: { open: () => ({ closed: of(true) }) } },
      ],
    });
    navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
  });

  afterEach(() => TestBed.resetTestingModule());

  describe('an existing set', () => {
    it('shows its fields in authored order', async () => {
      await open('intake', { ok: true, set: stubSet() });

      expect(input('field-0-name').value).toBe('deadline');
      expect(input('field-1-name').value).toBe('cost_share');
      expect(el.textContent).toContain('2 of 14 fields');
    });

    it('names the columns the form keeps but does not show', async () => {
      await open('intake', { ok: true, set: stubSet() });

      expect(el.textContent).toContain("a column the form doesn't show");
      expect(el.textContent).toContain('note');
      expect(el.textContent).toContain('- deadline must be in the future');
    });

    it('saves everything the form does not show, unchanged (§5.4)', async () => {
      await open('intake', { ok: true, set: stubSet() });

      await page().save();

      const body = lastBody() as FieldSetStructuredWrite;
      expect(service.write).toHaveBeenCalledWith('grants', 'intake', expect.anything());
      expect(body.unmanagedColumns).toEqual(['note']);
      expect(body.fields.map((f) => f.unmanagedValues)).toEqual([['ask Dana'], ['']]);
      expect(body.preamble).toBe('Read by the grants office.');
      expect(body.frontmatter).toEqual(['owner: Research Office']);
      expect(body.rules).toBe('- deadline must be in the future');
    });

    it('never changes the version on its own (§6)', async () => {
      await open('intake', { ok: true, set: stubSet() });
      type('field-1-find', 'Whether the funder requires cost sharing');

      await page().save();

      expect((lastBody() as FieldSetStructuredWrite).version).toBe('2');
    });

    it('offers a version bump only when the fields change shape', async () => {
      await open('intake', { ok: true, set: stubSet() });
      type('field-0-find', 'A different phrase');
      expect(el.textContent).not.toContain('Use version 3');

      page().moveField(1, -1);
      fixture.detectChanges();

      expect(el.textContent).toContain('Use version 3');
    });

    it('moves a field without sorting anything else (D4)', async () => {
      await open('intake', { ok: true, set: stubSet() });

      page().moveField(0, 1);
      fixture.detectChanges();
      await page().save();

      expect((lastBody() as FieldSetStructuredWrite).fields.map((f) => f.name)).toEqual([
        'cost_share',
        'deadline',
      ]);
    });

    it('sends allowed values only for an enum field', async () => {
      await open('intake', { ok: true, set: stubSet() });
      const select = el.querySelector('#field-1-type') as HTMLSelectElement;
      select.value = 'string';
      select.dispatchEvent(new Event('change'));
      fixture.detectChanges();

      await page().save();

      expect((lastBody() as FieldSetStructuredWrite).fields[1].enum).toEqual([]);
    });

    it('clears "Saved." at the next edit, so it never describes a file that has changed since', async () => {
      await open('intake', { ok: true, set: stubSet() });
      await page().save();
      fixture.detectChanges();
      expect(el.querySelector('[role="status"]')?.textContent).toContain('Saved.');

      type('field-0-find', 'Another phrase');

      expect(el.querySelector('[role="status"]')?.textContent?.trim()).toBe('');
    });

    it("shows the backend's refusal as written", async () => {
      await open('intake', { ok: true, set: stubSet() });
      service.write.mockRejectedValueOnce({ error: { detail: "field 'deadline': 'find' is required" } });

      await page().save();
      fixture.detectChanges();

      expect(el.querySelector('[role="alert"]')?.textContent).toContain("'find' is required");
    });

    it('renames by writing the new name, then deleting the old file', async () => {
      await open('intake', { ok: true, set: stubSet() });
      type('set-name', 'grant-intake');

      await page().save();

      expect(service.write).toHaveBeenCalledWith('grants', 'grant-intake', expect.anything());
      expect(service.remove).toHaveBeenCalledWith('grants', 'intake');
      expect(navigate).toHaveBeenCalledWith(
        ['/customize/skills', 'grants', 'field-sets', 'grant-intake'],
        { replaceUrl: true },
      );
    });

    it('will not rename onto a set that already exists', async () => {
      await open('intake', { ok: true, set: stubSet() });
      service.list.mockResolvedValue({
        skillId: 'grants',
        fieldSets: [{ name: 'budget', filename: 'field-set.budget.md', size: 1, contentHash: 'x' }],
        maxFields: 14,
      });
      type('set-name', 'budget');

      await page().save();

      expect(service.write).not.toHaveBeenCalled();
      expect(service.remove).not.toHaveBeenCalled();
    });

    it('deletes after confirming, then returns to the skill', async () => {
      await open('intake', { ok: true, set: stubSet() });

      await page().deleteSet();

      expect(service.remove).toHaveBeenCalledWith('grants', 'intake');
      expect(navigate).toHaveBeenCalledWith(['/customize/skills', 'grants', 'edit']);
    });
  });

  describe('markdown mode (D7)', () => {
    it('asks the backend to write the form, saving nothing', async () => {
      await open('intake', { ok: true, set: stubSet() });
      service.write.mockResolvedValueOnce(stubSet({ markdown: 'FROM THE SERVER' }));

      await page().showMarkdown();
      fixture.detectChanges();

      expect(service.write.mock.calls.at(-1)![3]).toBe(true);
      expect((el.querySelector('#set-markdown') as HTMLTextAreaElement).value).toBe('FROM THE SERVER');
    });

    it('stays in markdown when the text does not read as a set', async () => {
      await open('intake', { ok: true, set: stubSet() });
      await page().showMarkdown();
      service.write.mockRejectedValueOnce({ error: { detail: 'no field table found' } });

      await page().showForm();
      fixture.detectChanges();

      expect(el.querySelector('#set-markdown')).not.toBeNull();
      expect(el.querySelector('[role="alert"]')?.textContent).toContain(
        'no field table found. Fix it here, then switch.',
      );
    });

    it('saves the markdown as sent', async () => {
      await open('intake', { ok: true, set: stubSet() });
      await page().showMarkdown();
      fixture.detectChanges();
      const area = el.querySelector('#set-markdown') as HTMLTextAreaElement;
      area.value = '| name | find |\n|---|---|\n| x | y |\n';
      area.dispatchEvent(new Event('input'));

      await page().save();

      expect(lastBody()).toEqual({ markdown: '| name | find |\n|---|---|\n| x | y |\n' });
    });

    it('opens a stored file that no longer parses in markdown, with the reason (§4.1)', async () => {
      await open('legacy', {
        ok: false,
        error: { message: "'find' is required", markdown: '| name |\n|---|\n| x |\n' },
      });

      expect((el.querySelector('#set-markdown') as HTMLTextAreaElement).value).toBe('| name |\n|---|\n| x |\n');
      expect(el.querySelector('[role="alert"]')?.textContent).toContain("'find' is required");
    });
  });

  describe('a new set', () => {
    it('starts with one empty card and the ceiling from the backend', async () => {
      await open(undefined);

      expect(input('field-0-title')).not.toBeNull();
      expect(el.textContent).toContain('1 of 14 fields');
    });

    it('derives the field name from its title until the author edits it', async () => {
      await open(undefined);

      type('field-0-title', 'Budget Cap');
      expect(input('field-0-name').value).toBe('budget_cap');

      type('field-0-name', 'max_budget');
      type('field-0-title', 'Budget ceiling');
      expect(input('field-0-name').value).toBe('max_budget');
    });

    it('takes its name from the title when none is typed', async () => {
      await open(undefined);
      type('set-title', 'NSF Solicitation');
      type('field-0-title', 'Deadline');
      type('field-0-find', 'The deadline');

      await page().save();

      expect(service.write).toHaveBeenCalledWith('grants', 'nsf-solicitation', expect.anything());
      expect(service.remove).not.toHaveBeenCalled();
    });

    it('disables Add field at the ceiling and says why', async () => {
      service.list.mockResolvedValue({ skillId: 'grants', fieldSets: [], maxFields: 2 });
      await open(undefined);

      page().addField();
      fixture.detectChanges();

      const add = [...el.querySelectorAll('button')].find((b) => b.textContent?.includes('Add field'))!;
      expect(add.disabled).toBe(true);
      expect(el.textContent).toContain('2 fields is the most one set can hold');
    });
  });
});
