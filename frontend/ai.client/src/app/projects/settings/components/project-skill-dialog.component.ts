import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { Router } from '@angular/router';
import { HttpErrorResponse } from '@angular/common/http';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroChevronDown, heroCodeBracket, heroDocumentText, heroPencilSquare, heroPhoto } from '@ng-icons/heroicons/outline';
import { MarkdownComponent } from 'ngx-markdown';
import { firstValueFrom } from 'rxjs';
import { DialogShellComponent } from '../../../components/dialog/dialog-shell.component';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';
import { ProjectSkillContent, ProjectSkillFile } from '../../models/project.model';
import { ProjectApiService } from '../../services/project-api.service';
import { projectErrorMessage } from '../../services/projects.service';

export interface ProjectSkillDialogData {
  projectId: string;
  skillId: string;
  /** Shown as the title until the skill loads. */
  name: string;
}

/** Read-only: closing is the only outcome. */
export type ProjectSkillDialogResult = undefined;

/** Bundle directories, in the order agentskills.io lists them. */
const KIND_ORDER = ['reference', 'script', 'asset'] as const;
const KIND_HEADING: Record<string, string> = { reference: 'References', script: 'Scripts', asset: 'Assets' };
const KIND_ICON: Record<string, string> = { reference: 'heroDocumentText', script: 'heroCodeBracket', asset: 'heroPhoto' };

/** Above this a file is offered as unreadable here rather than dumped into the dialog. */
export const MAX_READABLE_BYTES = 512 * 1024;

const TEXT_EXTENSIONS = new Set(['md', 'markdown', 'txt', 'py', 'json', 'yaml', 'yml', 'csv', 'tsv', 'xml', 'js', 'ts', 'sh', 'toml', 'ini', 'html', 'css', 'sql']);

function extension(filename: string): string {
  const dot = filename.lastIndexOf('.');
  return dot < 0 ? '' : filename.slice(dot + 1).toLowerCase();
}

/** Whether a file is text the viewer can show. Judged on the name too, since a stored type can be generic. */
export function isReadableFile(file: ProjectSkillFile): boolean {
  if (file.size > MAX_READABLE_BYTES) return false;
  const type = (file.contentType || '').split(';')[0].trim().toLowerCase();
  if (type.startsWith('text/') || /json|yaml|xml|javascript|python|markdown/.test(type)) return true;
  return TEXT_EXTENSIONS.has(extension(file.filename));
}

function isMarkdown(filename: string): boolean {
  return ['md', 'markdown'].includes(extension(filename));
}

interface FileGroup {
  kind: string;
  heading: string;
  icon: string;
  files: ProjectSkillFile[];
}

type FileState = { status: 'loading' } | { status: 'loaded'; text: string } | { status: 'error'; message: string };

let nextInstance = 0;

/**
 * A project's bound skill, read-only: its description, the SKILL.md instructions, and
 * the files in its bundle, each readable in place. The content is the version the
 * project runs (`GET /projects/{id}/skills/{skillId}`), which is why this reads through
 * the project and not `/skills/{id}`: a member can read a private skill the editor who
 * added it wrote, which is in no one else's own list.
 *
 * Markdown renders through `ngx-markdown` **with sanitization on**. A skill can be
 * written by any user, so never add `[disableSanitizer]` here. Other text files show
 * verbatim in a `<pre>`.
 */
@Component({
  selector: 'app-project-skill-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, MarkdownComponent, NgIcon, SpinnerComponent],
  providers: [provideIcons({ heroChevronDown, heroCodeBracket, heroDocumentText, heroPencilSquare, heroPhoto })],
  template: `
    <app-dialog-shell [title]="title()" size="xl" (closed)="close()">
      @if (skill(); as s) {
        <!-- In the body, not the shell's fixed header: a skill's description can run to a
             paragraph, which would leave a phone-height dialog little room to scroll. -->
        @if (s.description) {
          <p class="text-sm/6 text-gray-700 dark:text-gray-300">{{ s.description }}</p>
        }
        <p class="mt-2 text-xs/5 text-gray-600 dark:text-gray-400">
          <span class="font-mono">{{ s.skillId }}</span>
          ·
          @if (s.version) {
            Version {{ s.version }}, the one this project runs
          } @else {
            Runs the latest version
          }
        </p>

        <section class="mt-5" [attr.aria-labelledby]="uid + '-instructions'">
          <h3 [id]="uid + '-instructions'" class="text-sm/6 font-semibold text-gray-900 dark:text-white">Instructions</h3>
          <p class="text-xs/5 text-gray-600 dark:text-gray-400">SKILL.md: what the assistant reads when it uses this skill.</p>
          @if (s.instructions) {
            <div class="message-block mt-2 overflow-x-auto rounded-2xl border border-gray-200 px-4 py-3 text-sm/6 text-gray-700 dark:border-gray-700 dark:text-gray-200">
              <markdown [data]="s.instructions" />
            </div>
          } @else {
            <p class="mt-2 text-sm/6 text-gray-600 dark:text-gray-400">This skill has no instructions, only its description.</p>
          }
        </section>

        <section class="mt-6" [attr.aria-labelledby]="uid + '-files'">
          <h3 [id]="uid + '-files'" class="flex items-baseline gap-2 text-sm/6 font-semibold text-gray-900 dark:text-white">
            Files
            <span class="text-xs/5 font-normal text-gray-600 tabular-nums dark:text-gray-400">{{ s.files.length }}</span>
          </h3>
          @if (!s.files.length) {
            <p class="text-sm/6 text-gray-600 dark:text-gray-400">No files beyond SKILL.md.</p>
          } @else {
            <p class="text-xs/5 text-gray-600 dark:text-gray-400">The assistant opens these when the instructions call for them.</p>
            @for (group of groups(); track group.kind) {
              <h4 class="mt-3 text-xs/5 font-semibold tracking-wide text-gray-600 uppercase dark:text-gray-300">
                {{ group.heading }} <span class="font-mono normal-case">({{ group.kind }}s/)</span>
              </h4>
              <ul class="mt-1 divide-y divide-gray-200 rounded-2xl border border-gray-200 dark:divide-gray-700 dark:border-gray-700">
                @for (file of group.files; track file.filename) {
                  <li>
                    <div class="flex items-center justify-between gap-3 px-4 py-2.5">
                      <span class="flex min-w-0 items-center gap-2">
                        <ng-icon [name]="group.icon" class="size-4 shrink-0 text-gray-500 dark:text-gray-400" aria-hidden="true" />
                        <span class="min-w-0">
                          <span class="block truncate font-mono text-sm/6 text-gray-900 dark:text-white">{{ file.filename }}</span>
                          <span class="block text-xs/5 text-gray-600 dark:text-gray-400">{{ sizeLabel(file.size) }}</span>
                        </span>
                      </span>
                      @if (readable(file)) {
                        <button
                          type="button"
                          (click)="toggleFile(file)"
                          [attr.aria-expanded]="isOpen(file)"
                          [attr.aria-controls]="fileRegionId(file)"
                          [attr.aria-label]="(isOpen(file) ? 'Hide ' : 'Read ') + file.filename"
                          class="inline-flex shrink-0 items-center gap-1 rounded-2xl px-2.5 py-1 text-xs/5 font-semibold text-primary-accessible hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark dark:hover:bg-gray-700"
                        >
                          {{ isOpen(file) ? 'Hide' : 'Read' }}
                          <ng-icon name="heroChevronDown" class="size-3.5 transition-transform" [class.rotate-180]="isOpen(file)" aria-hidden="true" />
                        </button>
                      } @else {
                        <span class="shrink-0 text-xs/5 text-gray-600 dark:text-gray-400">Can't be shown here</span>
                      }
                    </div>
                    @if (isOpen(file)) {
                      <div
                        [id]="fileRegionId(file)"
                        role="region"
                        [attr.aria-label]="file.filename"
                        class="border-t border-gray-200 bg-gray-50 px-4 py-3 dark:border-gray-700 dark:bg-gray-900/40"
                      >
                        @switch (fileState(file)?.status) {
                          @case ('loaded') {
                            @if (markdown(file)) {
                              <div class="message-block max-h-96 overflow-auto text-sm/6 text-gray-700 dark:text-gray-200">
                                <markdown [data]="fileText(file)" />
                              </div>
                            } @else {
                              <pre class="max-h-96 overflow-auto font-mono text-xs/5 whitespace-pre text-gray-800 dark:text-gray-200">{{ fileText(file) }}</pre>
                            }
                          }
                          @case ('error') {
                            <p role="alert" class="text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ fileError(file) }}</p>
                          }
                          @default {
                            <p class="flex items-center gap-2 text-sm/6 text-gray-600 dark:text-gray-400">
                              <app-spinner size="sm" [label]="'Loading ' + file.filename" /> Loading…
                            </p>
                          }
                        }
                      </div>
                    }
                  </li>
                }
              </ul>
            }
          }
        </section>

        @if (s.compose.length) {
          <section class="mt-6">
            <h3 class="text-sm/6 font-semibold text-gray-900 dark:text-white">Builds on</h3>
            <p class="mt-1 font-mono text-xs/5 text-gray-700 dark:text-gray-300">{{ s.compose.join(', ') }}</p>
          </section>
        }
      } @else if (error()) {
        <p role="alert" class="text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ error() }}</p>
      } @else {
        <p class="flex items-center gap-2 text-sm/6 text-gray-600 dark:text-gray-400">
          <app-spinner size="sm" label="Loading skill" /> Loading…
        </p>
      }

      <div dialogFooter class="flex items-center justify-between gap-3 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
        <p class="text-xs/5 text-gray-600 dark:text-gray-400">
          @if (skill()?.isOwned) {
            You wrote this skill. Edits reach this project once it’s updated to the new version.
          } @else {
            Read-only. Only its author can edit it.
          }
        </p>
        <div class="flex shrink-0 items-center gap-3">
          <button
            type="button"
            (click)="close()"
            class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-300 dark:hover:bg-gray-700 dark:hover:text-white"
          >
            Close
          </button>
          @if (skill()?.isOwned) {
            <!-- Editing lives on the authoring form; the viewer never grows a second editor. -->
            <button
              type="button"
              (click)="edit()"
              class="inline-flex items-center gap-1.5 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
            >
              <ng-icon name="heroPencilSquare" class="size-4" aria-hidden="true" />
              Edit skill
            </button>
          }
        </div>
      </div>
    </app-dialog-shell>
  `,
})
export class ProjectSkillDialogComponent {
  private dialogRef = inject<DialogRef<ProjectSkillDialogResult>>(DialogRef);
  private data = inject<ProjectSkillDialogData>(DIALOG_DATA);
  private api = inject(ProjectApiService);
  private router = inject(Router);

  protected readonly uid = `project-skill-${nextInstance++}`;
  protected readonly skill = signal<ProjectSkillContent | null>(null);
  protected readonly error = signal<string | null>(null);
  private readonly open = signal<ReadonlySet<string>>(new Set());
  private readonly files = signal<Readonly<Record<string, FileState>>>({});

  protected readonly title = computed(() => this.skill()?.displayName || this.data.name);

  /** References, then scripts, then assets; alphabetical within each. */
  protected readonly groups = computed<FileGroup[]>(() => {
    const files = this.skill()?.files ?? [];
    const kinds = [...KIND_ORDER, ...new Set(files.map(f => f.kind).filter(k => !(KIND_ORDER as readonly string[]).includes(k)))];
    return kinds
      .map(kind => ({
        kind,
        heading: KIND_HEADING[kind] ?? kind,
        icon: KIND_ICON[kind] ?? 'heroDocumentText',
        files: files.filter(f => f.kind === kind).sort((a, b) => a.filename.localeCompare(b.filename)),
      }))
      .filter(group => group.files.length);
  });

  constructor() {
    void this.load();
  }

  private async load(): Promise<void> {
    try {
      this.skill.set(await firstValueFrom(this.api.skillContent(this.data.projectId, this.data.skillId)));
    } catch (err) {
      this.error.set(skillErrorMessage(err));
    }
  }

  protected close(): void {
    this.dialogRef.close(undefined);
  }

  /**
   * To the skill's authoring form. Closed first, so the dialog never sits over the page it
   * leads to; the Skills page still asks before leaving unsaved checkbox changes.
   */
  protected edit(): void {
    const skill = this.skill();
    if (!skill?.isOwned) return;
    this.dialogRef.close(undefined);
    void this.router.navigate(['/customize/skills', skill.skillId, 'edit']);
  }

  protected readable(file: ProjectSkillFile): boolean {
    return isReadableFile(file);
  }

  protected markdown(file: ProjectSkillFile): boolean {
    return isMarkdown(file.filename);
  }

  protected isOpen(file: ProjectSkillFile): boolean {
    return this.open().has(file.filename);
  }

  protected fileState(file: ProjectSkillFile): FileState | undefined {
    return this.files()[file.filename];
  }

  protected fileText(file: ProjectSkillFile): string {
    const state = this.fileState(file);
    return state?.status === 'loaded' ? state.text : '';
  }

  protected fileError(file: ProjectSkillFile): string {
    const state = this.fileState(file);
    return state?.status === 'error' ? state.message : '';
  }

  protected fileRegionId(file: ProjectSkillFile): string {
    const index = this.skill()?.files.findIndex(f => f.filename === file.filename) ?? 0;
    return `${this.uid}-file-${index}`;
  }

  /** Open or close a file; the first open fetches it, and a failed fetch is retried on the next. */
  protected toggleFile(file: ProjectSkillFile): void {
    const name = file.filename;
    const next = new Set(this.open());
    if (next.has(name)) {
      next.delete(name);
      this.open.set(next);
      return;
    }
    next.add(name);
    this.open.set(next);
    const state = this.files()[name];
    if (!state || state.status === 'error') void this.loadFile(name);
  }

  private async loadFile(filename: string): Promise<void> {
    this.files.update(files => ({ ...files, [filename]: { status: 'loading' } }));
    try {
      const text = await firstValueFrom(this.api.skillFile(this.data.projectId, this.data.skillId, filename));
      this.files.update(files => ({ ...files, [filename]: { status: 'loaded', text } }));
    } catch (err) {
      this.files.update(files => ({
        ...files,
        [filename]: { status: 'error', message: projectErrorMessage(err, 'That file could not be loaded.') },
      }));
    }
  }

  protected sizeLabel(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
}

function skillErrorMessage(err: unknown): string {
  if (err instanceof HttpErrorResponse && err.status === 403) {
    return 'This skill isn’t available to you, so its content can’t be shown.';
  }
  if (err instanceof HttpErrorResponse && err.status === 404) {
    return 'This skill is no longer available.';
  }
  return projectErrorMessage(err, 'This skill could not be loaded.');
}
