import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
  OnInit,
} from '@angular/core';
import { RouterLink, Router, ActivatedRoute } from '@angular/router';
import { FormBuilder, FormGroup, Validators, ReactiveFormsModule } from '@angular/forms';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroPlus,
  heroTrash,
  heroEye,
  heroArrowUpTray,
  heroWrenchScrewdriver,
  heroDocumentText,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import { AdminSkillService } from '../services/admin-skill.service';
import { AdminToolService } from '../../tools/services/admin-tool.service';
import {
  SkillResourceRef,
  SkillStatus,
  SKILL_STATUSES,
  SKILL_CATEGORIES,
  SKILL_ID_PATTERN,
} from '../models/admin-skill.model';
import {
  parseSkillMarkdown,
  slugifySkillId,
} from '../models/skill-import.util';
import {
  ToolPickerDialogComponent,
  ToolPickerDialogData,
  ToolPickerDialogResult,
} from '../components/tool-picker-dialog.component';
import { parseScopedToolId } from '../../../shared/utils/scoped-tool-id';

@Component({
  selector: 'app-skill-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, ReactiveFormsModule, NgIcon],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroPlus,
      heroTrash,
      heroEye,
      heroArrowUpTray,
      heroWrenchScrewdriver,
      heroDocumentText,
      heroXMark,
    }),
  ],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-3xl px-4 py-8 sm:px-6 lg:px-8">
        <!-- Back link -->
        <a
          routerLink="/admin/skills"
          class="mb-6 inline-flex items-center gap-2 text-sm/6 font-medium text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
        >
          <ng-icon name="heroArrowLeft" class="size-4" aria-hidden="true" />
          Back to Skills
        </a>

        <!-- Page Header -->
        <div class="mb-8 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">
              {{ isEditMode() ? 'Edit Skill' : 'Create Skill' }}
            </h1>
            <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              {{ isEditMode() ? 'Update skill instructions, reference files and bound tools.' : 'Author a new skill, or import a SKILL.md to prefill it.' }}
            </p>
          </div>

          @if (!isEditMode()) {
            <div>
              <label
                for="skillImport"
                class="inline-flex cursor-pointer items-center gap-2 rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
              >
                <ng-icon name="heroArrowUpTray" class="size-4" aria-hidden="true" />
                Import SKILL.md
              </label>
              <input
                id="skillImport"
                type="file"
                accept=".md,.markdown,text/markdown,text/plain"
                class="sr-only"
                (change)="onImportSelected($event)"
              />
            </div>
          }
        </div>

        @if (loading()) {
          <div class="flex h-64 items-center justify-center">
            <div class="size-10 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-gray-700 dark:border-t-blue-500"></div>
          </div>
        } @else {
          @if (importNotice()) {
            <div class="mb-6 rounded-2xl border border-blue-200 bg-blue-50 p-3 text-sm/6 text-blue-800 dark:border-blue-800 dark:bg-blue-900/20 dark:text-blue-200">
              {{ importNotice() }}
            </div>
          }
          @if (error()) {
            <div class="mb-6 rounded-2xl border border-red-200 bg-red-50 p-3 text-sm/6 text-red-800 dark:border-red-800 dark:bg-red-900/20 dark:text-red-200">
              {{ error() }}
            </div>
          }

          <form [formGroup]="form" (ngSubmit)="onSubmit()" class="space-y-8">
            <!-- Basic Information -->
            <section class="space-y-4">
              <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">Basic information</h2>

              @if (!isEditMode()) {
                <div>
                  <label for="skillId" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Skill ID <span class="text-red-600">*</span>
                  </label>
                  <input
                    id="skillId"
                    type="text"
                    formControlName="skillId"
                    placeholder="e.g., pdf_workflows"
                    class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 font-mono text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                    [class.border-red-500]="form.get('skillId')?.invalid && form.get('skillId')?.touched"
                  />
                  @if (form.get('skillId')?.invalid && form.get('skillId')?.touched) {
                    <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">
                      Skill ID must be 3-50 characters: lowercase letters, numbers and underscores, starting with a letter.
                    </p>
                  }
                </div>
              }

              <div>
                <label for="displayName" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                  Display Name <span class="text-red-600">*</span>
                </label>
                <input
                  id="displayName"
                  type="text"
                  formControlName="displayName"
                  placeholder="e.g., PDF Workflows"
                  class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                  [class.border-red-500]="form.get('displayName')?.invalid && form.get('displayName')?.touched"
                />
                @if (form.get('displayName')?.invalid && form.get('displayName')?.touched) {
                  <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">Display name is required (1-100 characters).</p>
                }
              </div>

              <div>
                <label for="description" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                  Description <span class="text-red-600">*</span>
                </label>
                <textarea
                  id="description"
                  formControlName="description"
                  rows="2"
                  placeholder="One-line catalog summary the agent sees (token-cheap)…"
                  class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                  [class.border-red-500]="form.get('description')?.invalid && form.get('description')?.touched"
                ></textarea>
                @if (form.get('description')?.invalid && form.get('description')?.touched) {
                  <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">Description is required (max 500 characters).</p>
                }
              </div>

              <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <div>
                  <label for="status" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Status</label>
                  <select
                    id="status"
                    formControlName="status"
                    class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                  >
                    @for (status of statuses; track status.value) {
                      <option [value]="status.value">{{ status.label }}</option>
                    }
                  </select>
                </div>
                <div>
                  <label for="category" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Category</label>
                  <select
                    id="category"
                    formControlName="category"
                    class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                  >
                    <option value="">— None —</option>
                    @for (cat of categories; track cat.value) {
                      <option [value]="cat.value">{{ cat.label }}</option>
                    }
                  </select>
                </div>
              </div>
            </section>

            <!-- Instructions -->
            <section class="space-y-4">
              <div>
                <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">Instructions</h2>
                <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
                  The SKILL.md body, loaded when the agent activates this skill. Markdown.
                </p>
              </div>
              <textarea
                id="instructions"
                formControlName="instructions"
                rows="12"
                placeholder="# How to use these tools&#10;&#10;Describe the procedure the agent should follow…"
                class="block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 font-mono text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
              ></textarea>
            </section>

            <!-- Bound Tools -->
            <section class="space-y-4">
              <div class="flex items-center justify-between gap-3">
                <div>
                  <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">Bound tools</h2>
                  <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
                    Catalog tools this skill carries. The agent loads only these when the skill is active.
                  </p>
                </div>
                <button
                  type="button"
                  (click)="openToolPicker()"
                  class="inline-flex shrink-0 items-center gap-2 rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                >
                  <ng-icon name="heroWrenchScrewdriver" class="size-4" aria-hidden="true" />
                  Bind tools
                </button>
              </div>
              @if (boundToolIds().length > 0) {
                <div class="flex flex-wrap gap-1.5">
                  @for (toolId of boundToolIds(); track toolId) {
                    <span class="inline-flex items-center gap-1 rounded-2xl bg-blue-100 py-0.5 pl-2.5 pr-1 text-xs/5 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300">
                      <span class="font-mono" [title]="toolId">{{ toolDisplayName(toolId) }}</span>
                      <button
                        type="button"
                        (click)="removeTool(toolId)"
                        [attr.aria-label]="'Remove ' + toolId"
                        class="flex size-4 items-center justify-center rounded-full hover:bg-blue-200 dark:hover:bg-blue-800"
                      >
                        <ng-icon name="heroXMark" class="size-3" aria-hidden="true" />
                      </button>
                    </span>
                  }
                </div>
              } @else {
                <p class="text-sm/6 italic text-gray-500 dark:text-gray-400">No tools bound yet.</p>
              }
            </section>

            <!-- Reference Files -->
            <section class="space-y-4">
              <div>
                <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">Reference files</h2>
                <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
                  Supporting docs the agent can read on demand (markdown/text, up to 1&nbsp;MB each, 50 per skill).
                </p>
              </div>

              <div class="flex flex-wrap items-center gap-2">
                <label
                  for="refUpload"
                  class="inline-flex cursor-pointer items-center gap-2 rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                  [class.opacity-50]="resourceBusy()"
                >
                  <ng-icon name="heroArrowUpTray" class="size-4" aria-hidden="true" />
                  {{ isEditMode() ? 'Upload files' : 'Add files' }}
                </label>
                <input
                  id="refUpload"
                  type="file"
                  multiple
                  accept=".md,.markdown,.txt,text/markdown,text/plain"
                  class="sr-only"
                  [disabled]="resourceBusy()"
                  (change)="onRefFilesSelected($event)"
                />
                <button
                  type="button"
                  (click)="toggleNewFile()"
                  class="inline-flex items-center gap-2 rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                >
                  <ng-icon name="heroPlus" class="size-4" aria-hidden="true" />
                  New file
                </button>
              </div>

              <!-- Inline new-file authoring -->
              @if (showNewFile()) {
                <div class="space-y-2 rounded-2xl border border-gray-200 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-900/40">
                  <input
                    type="text"
                    [value]="newFileName()"
                    (input)="newFileName.set(asValue($event))"
                    placeholder="filename.md"
                    class="block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 font-mono text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                  />
                  <textarea
                    [value]="newFileContent()"
                    (input)="newFileContent.set(asValue($event))"
                    rows="6"
                    placeholder="# Reference content…"
                    class="block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 font-mono text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                  ></textarea>
                  <div class="flex justify-end gap-2">
                    <button
                      type="button"
                      (click)="toggleNewFile()"
                      class="rounded-2xl px-3 py-1.5 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700"
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      (click)="addNewFile()"
                      [disabled]="resourceBusy() || !newFileName().trim() || !newFileContent()"
                      class="rounded-2xl bg-blue-600 px-3 py-1.5 text-sm/6 font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-60 dark:bg-blue-500 dark:hover:bg-blue-600"
                    >
                      Add file
                    </button>
                  </div>
                </div>
              }

              <!-- Live manifest (edit mode) -->
              @if (isEditMode()) {
                @if (resources().length > 0) {
                  <ul class="divide-y divide-gray-200 overflow-hidden rounded-2xl border border-gray-200 dark:divide-gray-700 dark:border-gray-700">
                    @for (res of resources(); track res.filename) {
                      <li class="flex items-center gap-3 px-3 py-2">
                        <ng-icon name="heroDocumentText" class="size-4 shrink-0 text-gray-400 dark:text-gray-500" aria-hidden="true" />
                        <div class="min-w-0 flex-1">
                          <span class="block truncate font-mono text-sm/6 text-gray-900 dark:text-white">{{ res.filename }}</span>
                          <span class="text-xs/5 text-gray-500 dark:text-gray-400">{{ formatSize(res.size) }} · {{ res.contentType }}</span>
                        </div>
                        <button
                          type="button"
                          (click)="viewResource(res)"
                          [attr.aria-label]="'View ' + res.filename"
                          class="flex size-8 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                        >
                          <ng-icon name="heroEye" class="size-4" aria-hidden="true" />
                        </button>
                        <button
                          type="button"
                          (click)="deleteResource(res)"
                          [disabled]="resourceBusy()"
                          [attr.aria-label]="'Delete ' + res.filename"
                          class="flex size-8 items-center justify-center rounded-2xl text-gray-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-50 dark:text-gray-500 dark:hover:bg-red-900/20 dark:hover:text-red-400"
                        >
                          <ng-icon name="heroTrash" class="size-4" aria-hidden="true" />
                        </button>
                      </li>
                    }
                  </ul>
                } @else {
                  <p class="text-sm/6 italic text-gray-500 dark:text-gray-400">No reference files yet.</p>
                }

                @if (viewing(); as v) {
                  <div class="rounded-2xl border border-gray-200 dark:border-gray-700">
                    <div class="flex items-center justify-between border-b border-gray-200 px-3 py-2 dark:border-gray-700">
                      <span class="font-mono text-sm/6 text-gray-900 dark:text-white">{{ v.filename }}</span>
                      <button
                        type="button"
                        (click)="viewing.set(null)"
                        aria-label="Close preview"
                        class="flex size-7 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                      >
                        <ng-icon name="heroXMark" class="size-4" aria-hidden="true" />
                      </button>
                    </div>
                    <pre class="max-h-72 overflow-auto whitespace-pre-wrap px-3 py-2 font-mono text-xs/5 text-gray-700 dark:text-gray-300">{{ v.content }}</pre>
                  </div>
                }
              } @else {
                <!-- Staged files (create mode) -->
                @if (pendingFiles().length > 0) {
                  <ul class="divide-y divide-gray-200 overflow-hidden rounded-2xl border border-gray-200 dark:divide-gray-700 dark:border-gray-700">
                    @for (file of pendingFiles(); track file.name) {
                      <li class="flex items-center gap-3 px-3 py-2">
                        <ng-icon name="heroDocumentText" class="size-4 shrink-0 text-gray-400 dark:text-gray-500" aria-hidden="true" />
                        <div class="min-w-0 flex-1">
                          <span class="block truncate font-mono text-sm/6 text-gray-900 dark:text-white">{{ file.name }}</span>
                          <span class="text-xs/5 text-gray-500 dark:text-gray-400">{{ formatSize(file.size) }} · uploads after the skill is created</span>
                        </div>
                        <button
                          type="button"
                          (click)="removePendingFile(file.name)"
                          [attr.aria-label]="'Remove ' + file.name"
                          class="flex size-8 items-center justify-center rounded-2xl text-gray-400 hover:bg-red-50 hover:text-red-600 dark:text-gray-500 dark:hover:bg-red-900/20 dark:hover:text-red-400"
                        >
                          <ng-icon name="heroTrash" class="size-4" aria-hidden="true" />
                        </button>
                      </li>
                    }
                  </ul>
                } @else {
                  <p class="text-sm/6 italic text-gray-500 dark:text-gray-400">No reference files staged.</p>
                }
              }
            </section>

            <!-- Actions -->
            <div class="flex items-center justify-end gap-3 border-t border-gray-200 pt-6 dark:border-gray-700">
              <a
                routerLink="/admin/skills"
                class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 dark:text-gray-200 dark:hover:bg-gray-700"
              >
                Cancel
              </a>
              <button
                type="submit"
                [disabled]="saving() || form.invalid"
                class="inline-flex items-center gap-2 rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-60 dark:bg-blue-500 dark:hover:bg-blue-600"
              >
                {{ saving() ? 'Saving…' : (isEditMode() ? 'Update Skill' : 'Create Skill') }}
              </button>
            </div>
          </form>
        }
      </div>
    </div>
  `,
})
export class SkillFormPage implements OnInit {
  private fb = inject(FormBuilder);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private dialog = inject(Dialog);
  private adminSkillService = inject(AdminSkillService);
  private adminToolService = inject(AdminToolService);

  readonly statuses = SKILL_STATUSES;
  readonly categories = SKILL_CATEGORIES;

  loading = signal(false);
  saving = signal(false);
  error = signal<string | null>(null);
  importNotice = signal<string | null>(null);
  skillId = signal<string | null>(null);

  readonly isEditMode = computed(() => !!this.skillId());

  // Bound tools (managed via the picker dialog).
  readonly boundToolIds = signal<string[]>([]);

  // Reference files: live manifest in edit mode, staged Files in create mode.
  readonly resources = signal<SkillResourceRef[]>([]);
  readonly pendingFiles = signal<File[]>([]);
  readonly resourceBusy = signal(false);
  readonly viewing = signal<{ filename: string; content: string } | null>(null);

  // Inline new-file authoring.
  readonly showNewFile = signal(false);
  readonly newFileName = signal('');
  readonly newFileContent = signal('');

  form: FormGroup = this.fb.group({
    skillId: ['', [Validators.required, Validators.pattern(SKILL_ID_PATTERN)]],
    displayName: ['', [Validators.required, Validators.minLength(1), Validators.maxLength(100)]],
    description: ['', [Validators.required, Validators.maxLength(500)]],
    instructions: [''],
    status: ['active' as SkillStatus],
    category: [''],
  });

  async ngOnInit(): Promise<void> {
    const id = this.route.snapshot.paramMap.get('skillId');
    if (id) {
      this.skillId.set(id);
      // In edit mode the skillId control is irrelevant (hidden); clear its
      // validators so the form is valid.
      this.form.get('skillId')?.clearValidators();
      this.form.get('skillId')?.updateValueAndValidity();
      await this.loadSkill(id);
    }
  }

  async loadSkill(skillId: string): Promise<void> {
    this.loading.set(true);
    try {
      const skill = await this.adminSkillService.fetchSkill(skillId);
      this.form.patchValue({
        skillId: skill.skillId,
        displayName: skill.displayName,
        description: skill.description,
        instructions: skill.instructions,
        status: skill.status,
        category: skill.category ?? '',
      });
      this.boundToolIds.set([...skill.boundToolIds]);
      this.resources.set([...skill.resources]);
    } catch (err: unknown) {
      this.error.set(err instanceof Error ? err.message : 'Failed to load skill.');
    } finally {
      this.loading.set(false);
    }
  }

  // --- Helpers ---------------------------------------------------------------

  asValue(event: Event): string {
    return (event.target as HTMLInputElement | HTMLTextAreaElement).value;
  }

  toolDisplayName(toolId: string): string {
    // A scoped id (`base::mcpToolName`) binds one tool of a server — show
    // "Server · tool" so the chip reads cleanly.
    const { base, name } = parseScopedToolId(toolId);
    const serverName = this.adminToolService.getToolById(base)?.displayName ?? base;
    return name ? `${serverName} · ${name}` : serverName;
  }

  formatSize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  // --- Import ----------------------------------------------------------------

  async onImportSelected(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = ''; // allow re-importing the same file
    if (!file) return;

    try {
      const text = await file.text();
      const parsed = parseSkillMarkdown(text);
      const patch: Record<string, string> = {
        displayName: parsed.name || this.form.get('displayName')?.value || '',
        description: parsed.description || this.form.get('description')?.value || '',
        instructions: parsed.instructions || this.form.get('instructions')?.value || '',
      };
      if (parsed.name) {
        patch['skillId'] = slugifySkillId(parsed.name);
      }
      this.form.patchValue(patch);
      this.importNotice.set(
        'Imported from SKILL.md. Tool bindings are not imported — pick them below. Review the Skill ID, then add reference files.'
      );
    } catch {
      this.error.set('Could not read the selected SKILL.md file.');
    }
  }

  // --- Bound tools -----------------------------------------------------------

  async openToolPicker(): Promise<void> {
    const dialogRef = this.dialog.open<ToolPickerDialogResult>(ToolPickerDialogComponent, {
      data: { selectedToolIds: this.boundToolIds() } as ToolPickerDialogData,
    });
    const result = await firstValueFrom(dialogRef.closed);
    if (result !== undefined) {
      this.boundToolIds.set(result);
    }
  }

  removeTool(toolId: string): void {
    this.boundToolIds.update((ids) => ids.filter((id) => id !== toolId));
  }

  // --- Reference files -------------------------------------------------------

  toggleNewFile(): void {
    this.showNewFile.update((v) => !v);
    if (!this.showNewFile()) {
      this.newFileName.set('');
      this.newFileContent.set('');
    }
  }

  async addNewFile(): Promise<void> {
    const name = this.newFileName().trim();
    const content = this.newFileContent();
    if (!name || !content) return;
    const file = new File([content], name, { type: 'text/markdown' });
    await this.acceptFiles([file]);
    this.newFileName.set('');
    this.newFileContent.set('');
    this.showNewFile.set(false);
  }

  async onRefFilesSelected(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const files = input.files ? Array.from(input.files) : [];
    input.value = '';
    if (files.length > 0) {
      await this.acceptFiles(files);
    }
  }

  /**
   * Edit mode → upload each file immediately; create mode → stage it (uploaded
   * after the skill is created, since uploads need a skill_id).
   */
  private async acceptFiles(files: File[]): Promise<void> {
    if (this.isEditMode()) {
      const id = this.skillId()!;
      this.resourceBusy.set(true);
      this.error.set(null);
      try {
        let manifest = this.resources();
        for (const file of files) {
          manifest = await this.adminSkillService.uploadResource(id, file);
        }
        this.resources.set(manifest);
      } catch (err: unknown) {
        this.error.set(err instanceof Error ? err.message : 'Failed to upload reference file.');
      } finally {
        this.resourceBusy.set(false);
      }
    } else {
      this.pendingFiles.update((current) => {
        const byName = new Map(current.map((f) => [f.name, f]));
        for (const file of files) {
          byName.set(file.name, file); // replace same-named staged file
        }
        return Array.from(byName.values());
      });
    }
  }

  removePendingFile(name: string): void {
    this.pendingFiles.update((files) => files.filter((f) => f.name !== name));
  }

  async viewResource(res: SkillResourceRef): Promise<void> {
    const id = this.skillId();
    if (!id) return;
    this.resourceBusy.set(true);
    try {
      const content = await this.adminSkillService.readResource(id, res.filename);
      this.viewing.set({ filename: res.filename, content });
    } catch (err: unknown) {
      this.error.set(err instanceof Error ? err.message : 'Failed to read reference file.');
    } finally {
      this.resourceBusy.set(false);
    }
  }

  async deleteResource(res: SkillResourceRef): Promise<void> {
    const id = this.skillId();
    if (!id) return;
    if (!confirm(`Delete reference file "${res.filename}"?`)) return;
    this.resourceBusy.set(true);
    this.error.set(null);
    try {
      const manifest = await this.adminSkillService.deleteResource(id, res.filename);
      this.resources.set(manifest);
      if (this.viewing()?.filename === res.filename) {
        this.viewing.set(null);
      }
    } catch (err: unknown) {
      this.error.set(err instanceof Error ? err.message : 'Failed to delete reference file.');
    } finally {
      this.resourceBusy.set(false);
    }
  }

  // --- Submit ----------------------------------------------------------------

  async onSubmit(): Promise<void> {
    if (this.form.invalid) return;
    this.saving.set(true);
    this.error.set(null);
    try {
      const v = this.form.getRawValue();
      const category = v.category ? v.category : null;

      if (this.isEditMode()) {
        await this.adminSkillService.updateSkill(this.skillId()!, {
          displayName: v.displayName,
          description: v.description,
          instructions: v.instructions,
          status: v.status,
          category,
          boundToolIds: this.boundToolIds(),
        });
      } else {
        const created = await this.adminSkillService.createSkill({
          skillId: v.skillId,
          displayName: v.displayName,
          description: v.description,
          instructions: v.instructions,
          status: v.status,
          category,
          boundToolIds: this.boundToolIds(),
        });
        // Upload any staged reference files now that the skill exists.
        for (const file of this.pendingFiles()) {
          await this.adminSkillService.uploadResource(created.skillId, file);
        }
      }

      await this.router.navigate(['/admin/skills']);
    } catch (err: unknown) {
      console.error('Error saving skill:', err);
      this.error.set(this.describeSaveError(err));
    } finally {
      this.saving.set(false);
    }
  }

  private describeSaveError(err: unknown): string {
    if (err && typeof err === 'object' && 'error' in err) {
      const body = (err as { error?: unknown }).error;
      if (body && typeof body === 'object' && 'detail' in body) {
        const detail = (body as { detail?: unknown }).detail;
        if (typeof detail === 'string') return detail;
      }
    }
    return err instanceof Error ? err.message : 'Failed to save skill.';
  }
}
