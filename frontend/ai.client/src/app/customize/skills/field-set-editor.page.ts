import {
  ChangeDetectionStrategy,
  Component,
  Injector,
  OnInit,
  afterNextRender,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import { takeUntilDestroyed, toSignal } from '@angular/core/rxjs-interop';
import { Dialog } from '@angular/cdk/dialog';
import { FormArray, FormBuilder, FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowDown,
  heroArrowLeft,
  heroArrowUp,
  heroInformationCircle,
  heroPlus,
  heroTrash,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import { firstValueFrom } from 'rxjs';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../components/confirmation-dialog';
import {
  FIELD_TYPES,
  FIELD_TYPE_LABELS,
  FieldSet,
  FieldSetField,
  FieldSetStructuredWrite,
  fieldSetShape,
  suggestFieldName,
  suggestSetName,
} from './models/field-set.model';
import { FieldSetService } from './services/field-set.service';

type FieldGroup = FormGroup<{
  title: FormControl<string>;
  name: FormControl<string>;
  type: FormControl<string>;
  find: FormControl<string>;
  enum: FormControl<string[]>;
  optional: FormControl<boolean>;
  unmanagedValues: FormControl<string[]>;
  /** The name the field was loaded with; `null` for a field added here. */
  originalName: FormControl<string | null>;
  /** The author typed in the name box, so the title stops deriving it. */
  nameEdited: FormControl<boolean>;
}>;

/** What the form carries for the file but never shows as a control (§5.4). */
interface CarriedLayout {
  rules: string | null;
  preamble: string | null;
  postscript: string | null;
  frontmatter: string[];
  unmanagedColumns: string[];
}

/** The backend's message, ended as a sentence, then what to do about it. */
function withHint(message: string, hint: string): string {
  const trimmed = message.trim();
  return `${/[.!?]$/.test(trimmed) ? trimmed : `${trimmed}.`} ${hint}`;
}

const EMPTY_LAYOUT: CarriedLayout = {
  rules: null,
  preamble: null,
  postscript: null,
  frontmatter: [],
  unmanagedColumns: [],
};

/**
 * Create or edit one field set on a skill the user owns
 * (`docs/specs/field-set-authoring.md` §5).
 *
 * Fields are a vertical list of cards in authored order (D3). Order is changed
 * only by Move up / Move down, never sorted (D4). The page never parses or
 * writes the file (D2). Switching to markdown asks the backend to write the
 * form's file, and switching back asks it to read the markdown. Save sends
 * the structured set, or the markdown in markdown mode, and the backend's
 * message is the one shown when it refuses.
 *
 * What the form cannot show (`## Rules`, prose around the table, extra
 * frontmatter lines and unread columns) is carried on every structured save
 * unchanged, so saving from the form never deletes it (§5.4).
 */
@Component({
  selector: 'app-field-set-editor',
  imports: [ReactiveFormsModule, RouterLink, NgIcon],
  templateUrl: './field-set-editor.page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  viewProviders: [
    provideIcons({
      heroArrowDown,
      heroArrowLeft,
      heroArrowUp,
      heroInformationCircle,
      heroPlus,
      heroTrash,
      heroXMark,
    }),
  ],
})
export class FieldSetEditorPage implements OnInit {
  private fb = inject(FormBuilder);
  private router = inject(Router);
  private dialog = inject(Dialog);
  private injector = inject(Injector);
  private fieldSets = inject(FieldSetService);

  /** Bound from the route by `withComponentInputBinding()`. */
  readonly skillId = input.required<string>();
  /** The set's name, or absent on `…/field-sets/new`. */
  readonly name = input<string>();

  protected readonly fieldTypes = FIELD_TYPES;
  protected readonly fieldTypeLabels = FIELD_TYPE_LABELS;

  protected readonly isNew = computed(() => !this.name());
  protected readonly mode = signal<'form' | 'markdown'>('form');
  protected readonly loading = signal(true);
  protected readonly saving = signal(false);
  protected readonly switching = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly notice = signal<string | null>(null);
  /** The stored file could not be opened as a form (§4.1). */
  protected readonly unreadable = signal(false);
  protected readonly maxFields = signal<number | null>(null);

  /** The set as last loaded or saved — the baseline for "changed shape". */
  private readonly baseline = signal<FieldSet | null>(null);
  protected readonly carried = signal<CarriedLayout>(EMPTY_LAYOUT);

  protected readonly form = this.fb.nonNullable.group({
    setName: [''],
    title: [''],
    version: ['1'],
    fields: this.fb.array<FieldGroup>([]),
  });
  protected readonly markdown = new FormControl('', { nonNullable: true });

  private readonly formValue = toSignal(this.form.valueChanges, {
    initialValue: this.form.getRawValue(),
  });

  protected get fields(): FormArray<FieldGroup> {
    return this.form.controls.fields;
  }

  protected readonly fieldCount = computed(() => this.formValue().fields?.length ?? 0);
  protected readonly atCeiling = computed(() => {
    const max = this.maxFields();
    return max !== null && this.fieldCount() >= max;
  });

  /**
   * The field list changed shape (a name, type, allowed values, optional, or the
   * order) but the version did not. A no-op save never bumps the version (§6),
   * so the editor only suggests it, and only here.
   */
  protected readonly suggestBump = computed(() => {
    const loaded = this.baseline();
    const value = this.formValue();
    if (!loaded || this.mode() !== 'form') {
      return false;
    }
    const current = (value.fields ?? []).map((f) => ({
      name: (f.name ?? '').trim(),
      type: f.type ?? 'string',
      enum: f.type === 'enum' ? (f.enum ?? []) : [],
      optional: f.optional ?? false,
    }));
    return fieldSetShape(current) !== fieldSetShape(loaded.fields) && (value.version ?? '').trim() === loaded.version;
  });

  protected readonly nextVersion = computed(() => {
    const version = this.baseline()?.version ?? '';
    return /^\d+$/.test(version) ? String(Number(version) + 1) : null;
  });

  protected readonly renaming = computed(() => {
    const original = this.name();
    const current = (this.formValue().setName ?? '').trim();
    return !!original && current !== original;
  });

  constructor() {
    // "Saved." describes the file as it was saved; the next edit makes it stale.
    this.form.valueChanges.pipe(takeUntilDestroyed()).subscribe(() => this.notice.set(null));
    this.markdown.valueChanges.pipe(takeUntilDestroyed()).subscribe(() => this.notice.set(null));
  }

  ngOnInit(): void {
    // Inputs are bound by now; they are not in the constructor.
    void this.load();
  }

  // ---- load ---------------------------------------------------------------

  private async load(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    const skillId = this.skillId();
    const name = this.name();
    try {
      if (!name) {
        const listed = await this.fieldSets.list(skillId);
        this.maxFields.set(listed.maxFields);
        this.addField();
        return;
      }
      const result = await this.fieldSets.read(skillId, name);
      if (result.ok) {
        this.applySet(result.set);
        this.baseline.set(result.set);
        return;
      }
      // A broken stored file opens in markdown, with the parser's message, so
      // it can be fixed rather than refused (§4.1).
      this.unreadable.set(true);
      this.form.controls.setName.setValue(name);
      this.markdown.setValue(result.error.markdown ?? '');
      this.mode.set('markdown');
      this.error.set(result.error.message);
      const listed = await this.fieldSets.list(skillId);
      this.maxFields.set(listed.maxFields);
    } catch (err) {
      this.error.set(this.fieldSets.messageFor(err, "We couldn't load that field set. It may have been deleted."));
    } finally {
      this.loading.set(false);
    }
  }

  private applySet(set: FieldSet): void {
    this.maxFields.set(set.maxFields);
    this.form.controls.setName.setValue(set.name);
    this.form.controls.title.setValue(set.title);
    this.form.controls.version.setValue(set.version);
    this.fields.clear();
    for (const field of set.fields) {
      this.fields.push(this.fieldGroup(field, field.name));
    }
    this.carried.set({
      rules: set.rules,
      preamble: set.preamble,
      postscript: set.postscript,
      frontmatter: [...set.frontmatter],
      unmanagedColumns: [...set.unmanagedColumns],
    });
    this.markdown.setValue(set.markdown);
  }

  private fieldGroup(field: Partial<FieldSetField>, originalName: string | null): FieldGroup {
    const group: FieldGroup = this.fb.nonNullable.group({
      title: [field.title ?? ''],
      name: [field.name ?? ''],
      type: [field.type ?? 'string'],
      find: [field.find ?? ''],
      enum: [field.enum ?? ([] as string[])],
      optional: [field.optional ?? false],
      unmanagedValues: [field.unmanagedValues ?? ([] as string[])],
      originalName: this.fb.control<string | null>(originalName),
      nameEdited: [originalName !== null],
    });
    // Name follows the title until the author edits it (§5, the Name row).
    group.controls.title.valueChanges.subscribe((title) => {
      if (!group.controls.nameEdited.value) {
        group.controls.name.setValue(suggestFieldName(title), { emitEvent: true });
      }
    });
    return group;
  }

  // ---- cards ----------------------------------------------------------------

  protected addField(): void {
    if (this.atCeiling()) {
      return;
    }
    const columns = this.carried().unmanagedColumns.length;
    this.fields.push(this.fieldGroup({ unmanagedValues: Array<string>(columns).fill('') }, null));
    const index = this.fields.length - 1;
    afterNextRender(() => document.getElementById(`field-${index}-title`)?.focus(), {
      injector: this.injector,
    });
  }

  protected removeField(index: number): void {
    this.fields.removeAt(index);
  }

  protected moveField(index: number, delta: -1 | 1): void {
    const target = index + delta;
    if (target < 0 || target >= this.fields.length) {
      return;
    }
    const group = this.fields.at(index);
    this.fields.removeAt(index, { emitEvent: false });
    this.fields.insert(target, group);
    // Keep the keyboard on the card that moved. At an end its button is
    // disabled, so fall back to the other direction's button.
    const direction = delta < 0 ? 'up' : 'down';
    afterNextRender(
      () => {
        const atEnd = delta < 0 ? target === 0 : target === this.fields.length - 1;
        const id = `field-${target}-move-${atEnd ? (direction === 'up' ? 'down' : 'up') : direction}`;
        document.getElementById(id)?.focus();
      },
      { injector: this.injector },
    );
  }

  protected markNameEdited(index: number): void {
    this.fields.at(index).controls.nameEdited.setValue(true);
  }

  protected addEnumValue(index: number, input: HTMLInputElement): void {
    const value = input.value.trim();
    input.value = '';
    if (!value) {
      return;
    }
    const control = this.fields.at(index).controls.enum;
    if (!control.value.includes(value)) {
      control.setValue([...control.value, value]);
    }
  }

  protected onEnumKeydown(event: KeyboardEvent, index: number): void {
    if (event.key === 'Enter' || event.key === ',') {
      event.preventDefault();
      this.addEnumValue(index, event.target as HTMLInputElement);
    }
  }

  protected removeEnumValue(index: number, value: string): void {
    const control = this.fields.at(index).controls.enum;
    control.setValue(control.value.filter((v) => v !== value));
  }

  protected findPlaceholder(title: string): string {
    const subject = title.trim().toLowerCase();
    return subject
      ? `e.g. Where the document states the ${subject}, and what counts as it`
      : 'e.g. Where the document states this value, and what counts as it';
  }

  protected bumpVersion(): void {
    const next = this.nextVersion();
    if (next) {
      this.form.controls.version.setValue(next);
    }
  }

  // ---- markdown mode --------------------------------------------------------

  /** The structured body. Allowed values are sent only for an `enum` field. */
  private structuredBody(): FieldSetStructuredWrite {
    const value = this.form.getRawValue();
    const carried = this.carried();
    return {
      title: value.title,
      version: value.version,
      fields: value.fields.map((f) => ({
        name: f.name,
        title: f.title,
        type: f.type,
        find: f.find,
        enum: f.type === 'enum' ? f.enum : [],
        optional: f.optional,
        unmanagedValues: f.unmanagedValues,
      })),
      rules: carried.rules,
      preamble: carried.preamble,
      postscript: carried.postscript,
      frontmatter: carried.frontmatter,
      unmanagedColumns: carried.unmanagedColumns,
    };
  }

  /** The name to send: the one typed, else for a new set one from its title. */
  private targetName(): string {
    const typed = this.form.controls.setName.value.trim();
    return typed || suggestSetName(this.form.controls.title.value);
  }

  protected async showMarkdown(): Promise<void> {
    if (this.mode() === 'markdown' || this.switching()) {
      return;
    }
    this.switching.set(true);
    this.error.set(null);
    try {
      const preview = await this.fieldSets.write(
        this.skillId(),
        this.targetName() || 'draft',
        this.structuredBody(),
        true,
      );
      this.markdown.setValue(preview.markdown);
      this.mode.set('markdown');
    } catch (err) {
      this.error.set(
        withHint(this.fieldSets.messageFor(err, "The form couldn't be written as markdown"), 'Fix it in the form, then switch.'),
      );
    } finally {
      this.switching.set(false);
    }
  }

  protected async showForm(): Promise<void> {
    if (this.mode() === 'form' || this.switching()) {
      return;
    }
    this.switching.set(true);
    this.error.set(null);
    try {
      const parsed = await this.fieldSets.write(
        this.skillId(),
        this.targetName() || 'draft',
        { markdown: this.markdown.value },
        true,
      );
      const typedName = this.form.controls.setName.value;
      this.applySet(parsed);
      this.form.controls.setName.setValue(typedName);
      this.unreadable.set(false);
      this.mode.set('form');
    } catch (err) {
      this.error.set(
        withHint(this.fieldSets.messageFor(err, "This markdown doesn't read as a field set"), 'Fix it here, then switch.'),
      );
    } finally {
      this.switching.set(false);
    }
  }

  // ---- save, delete -----------------------------------------------------------

  protected async save(): Promise<void> {
    if (this.saving()) {
      return;
    }
    const skillId = this.skillId();
    const original = this.name() ?? null;
    const target = this.targetName();
    if (!target) {
      this.error.set('Give the field set a name.');
      return;
    }

    this.saving.set(true);
    this.error.set(null);
    this.notice.set(null);
    try {
      if (target !== original) {
        // A PUT replaces whatever is stored under the name, so a new set or a
        // rename must not land on one that already exists.
        const listed = await this.fieldSets.list(skillId);
        if (listed.fieldSets.some((s) => s.name === target)) {
          this.error.set(`This skill already has a field set named “${target}”. Choose another name.`);
          return;
        }
      }
      const body = this.mode() === 'markdown' ? { markdown: this.markdown.value } : this.structuredBody();
      const saved = await this.fieldSets.write(skillId, target, body);
      if (original && target !== original) {
        await this.fieldSets.remove(skillId, original);
      }
      this.unreadable.set(false);
      this.applySet(saved);
      this.baseline.set(saved);
      if (target !== original) {
        await this.router.navigate(['/customize/skills', skillId, 'field-sets', target], {
          replaceUrl: true,
        });
      }
      this.notice.set('Saved.');
    } catch (err) {
      this.error.set(this.fieldSets.messageFor(err, "The field set couldn't be saved."));
    } finally {
      this.saving.set(false);
    }
  }

  protected async deleteSet(): Promise<void> {
    const name = this.name();
    if (!name) {
      return;
    }
    const ref = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: 'Delete field set',
        message: `Delete “${this.form.controls.title.value || name}”? The agent will no longer be able to run it. This can't be undone.`,
        confirmText: 'Delete',
        destructive: true,
      } satisfies ConfirmationDialogData,
    });
    if ((await firstValueFrom(ref.closed)) !== true) {
      return;
    }
    try {
      await this.fieldSets.remove(this.skillId(), name);
      await this.router.navigate(['/customize/skills', this.skillId(), 'edit']);
    } catch (err) {
      this.error.set(this.fieldSets.messageFor(err, "The field set couldn't be deleted."));
    }
  }
}
