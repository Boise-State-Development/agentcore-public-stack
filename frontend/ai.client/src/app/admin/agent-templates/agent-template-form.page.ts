import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import {
  FormArray,
  FormControl,
  FormGroup,
  ReactiveFormsModule,
  Validators,
} from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowLeft, heroPlus, heroTrash } from '@ng-icons/heroicons/outline';
import { AdminAgentTemplatesService } from './services/admin-agent-templates.service';
import {
  AgentTemplateAdmin,
  AgentTemplateCreate,
  TemplateStatus,
} from './models/agent-template-admin.model';
import { AgentService } from '../../agents/services/agent.service';
import { BindableItem } from '../../agents/models/agent.model';
import { EmojiPickerComponent } from '../../shared/emoji-picker/emoji-picker.component';

const MAX_NAME = 128;
const MAX_DESCRIPTION = 1024;
const MAX_INSTRUCTIONS = 100_000;
const MAX_PITCH = 200;

/** Binding kinds a template can carry (free string on the wire; these are the known set). */
const BINDING_KINDS = ['tool', 'knowledge_base', 'skill', 'memory_space'] as const;

type BindingGroup = FormGroup<{
  kind: FormControl<string>;
  ref: FormControl<string>;
  config: FormControl<Record<string, unknown>>;
}>;

/**
 * Create / edit an agent template. Mirrors the `agent-form` field set (name,
 * emoji, description, instructions, tags, starters, model, tool
 * bindings) plus the catalog-management fields (`pitch`, `status`,
 * `sort_order`). POST on create, PATCH on edit — same shape either way.
 *
 * `modelId` blank ⇒ platform default (`modelConfig.modelId = null`), matching
 * the TemplateDraft contract. Model `params` and any binding `config` loaded
 * from an existing template are preserved through the form even though this
 * admin UI does not expose an editor for them.
 */
@Component({
  selector: 'app-agent-template-form-page',
  imports: [RouterLink, ReactiveFormsModule, NgIcon, EmojiPickerComponent],
  providers: [provideIcons({ heroArrowLeft, heroPlus, heroTrash })],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="max-w-2xl">
      <a
        routerLink="/admin/agent-templates"
        class="mb-6 inline-flex items-center gap-2 text-sm/6 font-medium text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
      >
        <ng-icon name="heroArrowLeft" class="size-4" />
        Back to Agent Templates
      </a>

      <h1 class="mb-6 text-2xl/8 font-bold text-gray-900 dark:text-white">
        {{ isEdit() ? 'Edit Agent Template' : 'New Agent Template' }}
      </h1>

      @if (loadError()) {
        <div class="mb-4 rounded-sm border border-state-danger-300 bg-state-danger-50 p-4 text-sm/6 text-state-danger-700 dark:border-state-danger-700 dark:bg-state-danger-900/20 dark:text-state-danger-300">
          {{ loadError() }}
        </div>
      }

      <form [formGroup]="form" (ngSubmit)="onSubmit()" class="space-y-5" novalidate>
        <!-- Name + emoji -->
        <div class="flex gap-3">
          <div class="w-24">
            <label class="mb-1.5 block text-sm/6 font-medium text-gray-900 dark:text-white">Emoji</label>
            <app-emoji-picker formControlName="emoji" />
          </div>
          <div class="flex-1">
            <label for="name" class="mb-1.5 block text-sm/6 font-medium text-gray-900 dark:text-white">
              Name <span aria-hidden="true" class="text-state-danger-500">*</span>
            </label>
            <input
              id="name"
              type="text"
              formControlName="name"
              maxlength="128"
              placeholder="e.g. Document Q&A Assistant"
              class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
              [class.border-state-danger-500]="form.controls.name.invalid && form.controls.name.touched"
            />
            @if (form.controls.name.invalid && form.controls.name.touched) {
              <p class="mt-1 text-xs/5 text-state-danger-600 dark:text-state-danger-400" role="alert">Name is required (max 128 characters).</p>
            }
          </div>
        </div>

        <!-- Slug (create only) -->
        @if (!isEdit()) {
          <div>
            <label for="slug" class="mb-1.5 block text-sm/6 font-medium text-gray-900 dark:text-white">Template ID (slug)</label>
            <input
              id="slug"
              type="text"
              formControlName="slug"
              maxlength="128"
              placeholder="Leave blank to derive from the name"
              class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 font-mono text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
            />
            <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">A stable handle. Slugified from the name if left blank.</p>
          </div>
        }

        <!-- Pitch -->
        <div>
          <label for="pitch" class="mb-1.5 block text-sm/6 font-medium text-gray-900 dark:text-white">Picker blurb (pitch)</label>
          <input
            id="pitch"
            type="text"
            formControlName="pitch"
            [maxlength]="maxPitch"
            placeholder="One line shown next to the template in the picker"
            class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
          />
        </div>

        <!-- Description -->
        <div>
          <label for="description" class="mb-1.5 block text-sm/6 font-medium text-gray-900 dark:text-white">Description</label>
          <input
            id="description"
            type="text"
            formControlName="description"
            [maxlength]="maxDescription"
            placeholder="What this template is for"
            class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
          />
        </div>

        <!-- Instructions -->
        <div>
          <label for="instructions" class="mb-1.5 block text-sm/6 font-medium text-gray-900 dark:text-white">Instructions</label>
          <p class="mb-1.5 text-xs/5 text-gray-500 dark:text-gray-400">
            The agent's system prompt, pre-filled into the create form. Write it Save-able as-is — no bracket placeholders. Guardrail wording here is an example to adapt.
          </p>
          <textarea
            id="instructions"
            formControlName="instructions"
            rows="10"
            [maxlength]="maxInstructions"
            placeholder="You are a helpful assistant that..."
            class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 font-mono text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
          ></textarea>
        </div>

        <!-- Model -->
        <div>
          <label for="modelId" class="mb-1.5 block text-sm/6 font-medium text-gray-900 dark:text-white">Default model</label>
          <select
            id="modelId"
            formControlName="modelId"
            class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          >
            <option value="">Platform default</option>
            @for (m of models(); track m.ref) {
              <option [value]="m.ref">{{ m.label }}</option>
            }
            @if (staleModelId()) {
              <option [value]="staleModelId()">{{ staleModelId() }} — unavailable</option>
            }
          </select>
          @if (staleModelId()) {
            <p class="mt-1 text-xs/5 text-state-warning-600 dark:text-state-warning-400" role="alert">
              This template's saved model (<code>{{ staleModelId() }}</code>) is no longer in the model catalog. It's kept selected as “unavailable” — pick a listed model or Platform default to replace it.
            </p>
          } @else {
            <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
              Platform default lets each agent use the deployment's default model. Pick a specific model to pin it.
            </p>
          }
        </div>

        <!-- Tags -->
        <div>
          <label for="tags" class="mb-1.5 block text-sm/6 font-medium text-gray-900 dark:text-white">Tags</label>
          <input
            id="tags"
            type="text"
            formControlName="tagsCsv"
            placeholder="comma, separated, tags"
            class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
          />
        </div>

        <!-- Starters -->
        <div formArrayName="starters">
          <div class="mb-1.5 flex items-center justify-between">
            <label class="block text-sm/6 font-medium text-gray-900 dark:text-white">Conversation starters</label>
            <button type="button" (click)="addStarter()" class="inline-flex items-center gap-1 text-xs/5 font-medium text-primary-accessible hover:underline dark:text-primary-accessible-dark">
              <ng-icon name="heroPlus" class="size-4" /> Add
            </button>
          </div>
          @for (ctrl of starters.controls; track $index) {
            <div class="mb-2 flex items-center gap-2">
              <input
                [formControlName]="$index"
                type="text"
                placeholder="A suggested opening prompt"
                class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
              />
              <button type="button" (click)="removeStarter($index)" class="shrink-0 rounded-2xl border border-gray-300 p-1.5 text-gray-500 hover:bg-gray-100 dark:border-gray-500 dark:hover:bg-gray-600" [attr.aria-label]="'Remove starter ' + ($index + 1)">
                <ng-icon name="heroTrash" class="size-4" />
              </button>
            </div>
          }
        </div>

        <!-- Bindings -->
        <div formArrayName="bindings">
          <div class="mb-1.5 flex items-center justify-between">
            <label class="block text-sm/6 font-medium text-gray-900 dark:text-white">Tool &amp; capability bindings</label>
            <button type="button" (click)="addBinding()" class="inline-flex items-center gap-1 text-xs/5 font-medium text-primary-accessible hover:underline dark:text-primary-accessible-dark">
              <ng-icon name="heroPlus" class="size-4" /> Add
            </button>
          </div>
          <p class="mb-2 text-xs/5 text-gray-500 dark:text-gray-400">
            Tool bindings pick from the enabled tool catalog. Other kinds name an intended
            capability by ref; an unknown ref is dropped gracefully at prefill time, so a
            binding a fork lacks never breaks the template.
          </p>
          @for (group of bindings.controls; track $index) {
            <div class="mb-2 flex items-center gap-2" [formGroupName]="$index">
              <select
                formControlName="kind"
                class="shrink-0 rounded-sm border border-gray-300 bg-white px-2 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
              >
                @for (k of bindingKinds; track k) {
                  <option [value]="k">{{ k }}</option>
                }
              </select>
              @if (group.controls.kind.value === 'tool') {
                <select
                  formControlName="ref"
                  class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                >
                  <option value="">Select a tool…</option>
                  @for (t of tools(); track t.ref) {
                    <option [value]="t.ref">{{ t.label }}</option>
                  }
                  @if (group.controls.ref.value && !isKnownToolRef(group.controls.ref.value)) {
                    <option [value]="group.controls.ref.value">{{ group.controls.ref.value }} — unavailable</option>
                  }
                </select>
              } @else {
                <input
                  formControlName="ref"
                  type="text"
                  placeholder="capability ref"
                  class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 font-mono text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                />
              }
              <button type="button" (click)="removeBinding($index)" class="shrink-0 rounded-2xl border border-gray-300 p-1.5 text-gray-500 hover:bg-gray-100 dark:border-gray-500 dark:hover:bg-gray-600" [attr.aria-label]="'Remove binding ' + ($index + 1)">
                <ng-icon name="heroTrash" class="size-4" />
              </button>
            </div>
          }
        </div>

        <!-- Status + order -->
        <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <label for="status" class="mb-1.5 block text-sm/6 font-medium text-gray-900 dark:text-white">Status</label>
            <select id="status" formControlName="status" class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white">
              <option value="enabled">Enabled — shown in picker</option>
              <option value="disabled">Disabled — hidden</option>
            </select>
          </div>
          <div>
            <label for="sortOrder" class="mb-1.5 block text-sm/6 font-medium text-gray-900 dark:text-white">Sort order</label>
            <input id="sortOrder" type="number" formControlName="sortOrder" class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white" />
          </div>
        </div>

        @if (submitError()) {
          <div class="rounded-sm border border-state-danger-300 bg-state-danger-50 p-3 text-sm/6 text-state-danger-700 dark:border-state-danger-700 dark:bg-state-danger-900/20 dark:text-state-danger-300" role="alert">
            {{ submitError() }}
          </div>
        }

        <div class="flex items-center gap-3 pt-2">
          <button
            type="submit"
            [disabled]="form.invalid || saving()"
            class="inline-flex items-center gap-2 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus:outline-none focus:ring-2 focus:ring-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {{ saving() ? 'Saving…' : (isEdit() ? 'Save changes' : 'Create template') }}
          </button>
          <a routerLink="/admin/agent-templates" class="text-sm/6 font-medium text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white">Cancel</a>
        </div>
      </form>
    </div>
  `,
})
export class AgentTemplateFormPage implements OnInit {
  private readonly service = inject(AdminAgentTemplatesService);
  private readonly agents = inject(AgentService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  protected readonly maxPitch = MAX_PITCH;
  protected readonly maxDescription = MAX_DESCRIPTION;
  protected readonly maxInstructions = MAX_INSTRUCTIONS;
  protected readonly bindingKinds = BINDING_KINDS;

  protected readonly isEdit = signal(false);
  protected readonly saving = signal(false);
  protected readonly loadError = signal<string | null>(null);
  protected readonly submitError = signal<string | null>(null);

  /** RBAC-filtered model palette (`GET /agents/bindable?kind=model`, reused via AgentService). */
  protected readonly models = signal<BindableItem[]>([]);
  /** RBAC-filtered tool palette (`GET /agents/bindable?kind=tool`, reused via AgentService). */
  protected readonly tools = signal<BindableItem[]>([]);
  /**
   * A saved `modelId` that is NOT in the current catalog (stale/removed model). Rendered as
   * a clearly-labelled "unavailable" option so an edit doesn't silently drop the admin's
   * choice. `null` when the saved model is present, blank/default, or on create.
   */
  protected readonly staleModelId = signal<string | null>(null);

  /** Model params preserved from an existing template (no UI editor for them). */
  private modelParams: Record<string, unknown> = {};

  protected readonly form = new FormGroup({
    slug: new FormControl('', { nonNullable: true }),
    name: new FormControl('', { nonNullable: true, validators: [Validators.required, Validators.maxLength(MAX_NAME)] }),
    emoji: new FormControl('', { nonNullable: true, validators: [Validators.maxLength(16)] }),
    pitch: new FormControl('', { nonNullable: true, validators: [Validators.maxLength(MAX_PITCH)] }),
    description: new FormControl('', { nonNullable: true, validators: [Validators.maxLength(MAX_DESCRIPTION)] }),
    instructions: new FormControl('', { nonNullable: true, validators: [Validators.maxLength(MAX_INSTRUCTIONS)] }),
    modelId: new FormControl('', { nonNullable: true }),
    tagsCsv: new FormControl('', { nonNullable: true }),
    status: new FormControl<TemplateStatus>('enabled', { nonNullable: true }),
    sortOrder: new FormControl(0, { nonNullable: true }),
    starters: new FormArray<FormControl<string>>([]),
    bindings: new FormArray<BindingGroup>([]),
  });

  protected get starters(): FormArray<FormControl<string>> {
    return this.form.controls.starters;
  }

  protected get bindings(): FormArray<BindingGroup> {
    return this.form.controls.bindings;
  }

  async ngOnInit(): Promise<void> {
    // Load the model + tool palettes first so the dropdowns are populated and the
    // edit-mode preselect (and stale detection) can compare against them.
    const [models, tools] = await Promise.all([
      this.agents.loadBindable('model'),
      this.agents.loadBindable('tool'),
    ]);
    this.models.set(models);
    this.tools.set(tools);

    const templateId = this.route.snapshot.paramMap.get('id');
    if (!templateId) return;

    this.isEdit.set(true);
    try {
      const tpl = await this.service.getTemplate(templateId);
      this.populate(tpl);
    } catch (err) {
      this.loadError.set(err instanceof Error ? err.message : 'Failed to load template.');
    }
  }

  private populate(tpl: AgentTemplateAdmin): void {
    this.modelParams = tpl.modelConfig?.params ?? {};
    const savedModelId = tpl.modelConfig?.modelId ?? '';
    this.form.patchValue({
      name: tpl.name,
      emoji: tpl.emoji,
      pitch: tpl.pitch,
      description: tpl.description,
      instructions: tpl.instructions,
      modelId: savedModelId,
      tagsCsv: (tpl.tags ?? []).join(', '),
      status: tpl.status,
      sortOrder: tpl.sort_order,
    });

    // A saved model that isn't in the current catalog is surfaced as an "unavailable"
    // option rather than silently dropped, so the dropdown can keep it selected.
    this.staleModelId.set(
      savedModelId && !this.models().some(m => m.ref === savedModelId) ? savedModelId : null,
    );

    this.starters.clear();
    for (const s of tpl.starters ?? []) {
      this.starters.push(new FormControl(s, { nonNullable: true }));
    }

    this.bindings.clear();
    for (const b of tpl.bindings ?? []) {
      this.bindings.push(this.makeBinding(b.kind, b.ref, b.config ?? {}));
    }
  }

  private makeBinding(kind: string, ref: string, config: Record<string, unknown>): BindingGroup {
    return new FormGroup({
      kind: new FormControl(kind || 'tool', { nonNullable: true }),
      ref: new FormControl(ref, { nonNullable: true }),
      config: new FormControl<Record<string, unknown>>(config, { nonNullable: true }),
    });
  }

  protected addStarter(): void {
    this.starters.push(new FormControl('', { nonNullable: true }));
  }

  protected removeStarter(index: number): void {
    this.starters.removeAt(index);
  }

  protected addBinding(): void {
    this.bindings.push(this.makeBinding('tool', '', {}));
  }

  /**
   * Is this tool ref in the current catalog? A tool binding whose ref is not known
   * (removed/renamed tool) is shown as an "unavailable" option rather than dropped, so
   * an edit preserves the admin's saved ref on save.
   */
  protected isKnownToolRef(ref: string): boolean {
    return this.tools().some(t => t.ref === ref);
  }

  protected removeBinding(index: number): void {
    this.bindings.removeAt(index);
  }

  /** Build the create/update payload from the form. */
  protected buildPayload(): AgentTemplateCreate {
    const raw = this.form.getRawValue();
    const modelId = raw.modelId.trim();
    const payload: AgentTemplateCreate = {
      name: raw.name.trim(),
      emoji: raw.emoji.trim(),
      pitch: raw.pitch.trim(),
      description: raw.description.trim(),
      instructions: raw.instructions,
      status: raw.status,
      sort_order: Number(raw.sortOrder) || 0,
      tags: raw.tagsCsv
        .split(',')
        .map(t => t.trim())
        .filter(t => t.length > 0),
      starters: raw.starters.map(s => s.trim()).filter(s => s.length > 0),
      modelConfig: { modelId: modelId.length > 0 ? modelId : null, params: this.modelParams },
      bindings: raw.bindings
        .map(b => ({ kind: b.kind, ref: b.ref.trim(), config: b.config ?? {} }))
        .filter(b => b.ref.length > 0),
    };
    const slug = raw.slug.trim();
    if (!this.isEdit() && slug.length > 0) {
      payload.template_id = slug;
    }
    return payload;
  }

  async onSubmit(): Promise<void> {
    if (this.form.invalid || this.saving()) return;
    this.submitError.set(null);
    this.saving.set(true);

    const payload = this.buildPayload();
    const templateId = this.route.snapshot.paramMap.get('id');

    try {
      if (templateId) {
        await this.service.updateTemplate(templateId, payload);
      } else {
        await this.service.createTemplate(payload);
      }
      await this.router.navigate(['/admin/agent-templates']);
    } catch (err) {
      this.submitError.set(
        err instanceof Error ? err.message : 'Failed to save template. Please try again.',
      );
    } finally {
      this.saving.set(false);
    }
  }
}
