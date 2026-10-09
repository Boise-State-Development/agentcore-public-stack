import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import { FormBuilder, ReactiveFormsModule } from '@angular/forms';
import { toSignal } from '@angular/core/rxjs-interop';
import { Dialog } from '@angular/cdk/dialog';
import { HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroKey, heroExclamationTriangle } from '@ng-icons/heroicons/outline';

import { UserGrant, UserGrantUpdate } from '../models';
import { UserGrantHttpService } from '../services/user-grant-http.service';
import { AdminToolService } from '../../tools/services/admin-tool.service';
import { ManagedModelsService } from '../../manage-models/services/managed-models.service';
import { AdminSkillService } from '../../skills/services/admin-skill.service';
import { ToolSelectorComponent } from '../../../components/tool-selector/tool-selector.component';
import { ToolSelectorItem } from '../../../components/tool-selector/tool-selector.model';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../../components/confirmation-dialog/confirmation-dialog.component';
import { ToastService } from '../../../services/toast/toast.service';
import { isRetiring } from '../../../shared/utils/retirement';
import { parseIso } from '../../../utils/date';

/** The empty shape the API serves for a user with no grant. */
function emptyGrant(userId: string): UserGrant {
  return {
    userId,
    grantedTools: [],
    grantedModels: [],
    grantedSkills: [],
    expiresAt: null,
    note: '',
    grantedBy: null,
    createdAt: '',
    updatedAt: '',
    active: true,
  };
}

/** ISO 8601 → the local `YYYY-MM-DDTHH:mm` a `datetime-local` input takes. */
export function isoToLocalInput(iso: string | null): string {
  if (!iso) return '';
  const d = parseIso(iso);
  if (isNaN(d.getTime())) return '';
  const pad = (n: number) => n.toString().padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** The inverse: a `datetime-local` value (local time) → ISO 8601 UTC, or null when blank. */
export function localInputToIso(value: string): string | null {
  if (!value) return null;
  const d = new Date(value);
  return isNaN(d.getTime()) ? null : d.toISOString();
}

function sameList(a: readonly string[], b: ReadonlySet<string>): boolean {
  return a.length === b.size && a.every((id) => b.has(id));
}

/**
 * The **Direct access** card on a user's admin detail page: tools, models and
 * skills granted to this one user beside their roles, drawn with the same
 * selector the role form uses.
 *
 * The three pickers edit three selection signals; Save sends all of them as a
 * full replace (`PUT`), matching the backend's replace semantics so the stored
 * grant is exactly what is on screen. Saving with nothing ticked removes the
 * grant, as does **Remove grant** — the API has one representation of "no
 * direct grant", and so does this card.
 */
@Component({
  selector: 'app-user-access-section',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReactiveFormsModule, NgIcon, ToolSelectorComponent],
  providers: [provideIcons({ heroKey, heroExclamationTriangle })],
  host: { class: 'block' },
  template: `
    <section
      class="p-6 bg-white border border-gray-300 rounded-sm dark:bg-gray-800 dark:border-gray-600"
      aria-labelledby="user-access-heading"
    >
      <div class="flex items-start justify-between gap-4 mb-2">
        <div class="flex items-center gap-2">
          <ng-icon name="heroKey" class="size-5 text-primary-600" aria-hidden="true" />
          <h3 id="user-access-heading" class="font-semibold">Direct access</h3>
        </div>
        @if (hasGrant()) {
          @if (grant().active) {
            <span class="px-2 py-0.5 text-xs rounded-xs bg-state-success-100 text-state-success-800 dark:bg-state-success-900 dark:text-state-success-200">
              Direct grant active
            </span>
          } @else {
            <span class="px-2 py-0.5 text-xs rounded-xs bg-state-warning-100 text-state-warning-800 dark:bg-state-warning-900 dark:text-state-warning-200">
              Expired
            </span>
          }
        }
      </div>
      <p id="user-access-blurb" class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
        Tools, models and skills this user gets in addition to their roles. A grant only
        adds; to take something away from everyone, disable it in its catalog.
      </p>

      @if (unavailable()) {
        <p class="text-sm/6 text-gray-600 dark:text-gray-400">
          Direct grants are not turned on in this environment.
        </p>
      } @else if (loading()) {
        <p class="text-sm/6 text-gray-600 dark:text-gray-400">Loading direct access...</p>
      } @else {
        @if (error(); as message) {
          <div
            role="alert"
            class="mb-4 p-3 bg-state-danger-50 border border-state-danger-200 rounded-sm text-sm/6 text-state-danger-800 dark:bg-state-danger-900/20 dark:border-state-danger-800 dark:text-state-danger-200"
          >
            {{ message }}
          </div>
        }

        <div class="space-y-6">
          <div>
            <h4 id="user-access-tools-heading" class="mb-2 text-sm/6 font-medium text-gray-900 dark:text-white">
              Tools
            </h4>
            @if (toolService.toolsResource.isLoading()) {
              <p class="text-sm/6 text-gray-600 dark:text-gray-400">Loading tools...</p>
            } @else {
              <app-tool-selector
                [items]="toolItems()"
                [selected]="tools()"
                (selectedChange)="tools.set($event)"
                labelledBy="user-access-tools-heading"
                describedBy="user-access-blurb"
                emptyText="No tools in the catalog."
                maxHeight="sm"
              />
            }
          </div>

          <div>
            <h4 id="user-access-models-heading" class="mb-2 text-sm/6 font-medium text-gray-900 dark:text-white">
              Models
            </h4>
            @if (modelService.modelsResource.isLoading()) {
              <p class="text-sm/6 text-gray-600 dark:text-gray-400">Loading models...</p>
            } @else {
              <app-tool-selector
                [items]="modelItems()"
                [selected]="models()"
                (selectedChange)="models.set($event)"
                labelledBy="user-access-models-heading"
                describedBy="user-access-blurb"
                noun="model"
                nounPlural="models"
                emptyText="No models in the catalog."
                maxHeight="sm"
              />
            }
          </div>

          <div>
            <h4 id="user-access-skills-heading" class="mb-2 text-sm/6 font-medium text-gray-900 dark:text-white">
              Skills
            </h4>
            @if (skillService.skillsResource.isLoading()) {
              <p class="text-sm/6 text-gray-600 dark:text-gray-400">Loading skills...</p>
            } @else {
              <app-tool-selector
                [items]="skillItems()"
                [selected]="skills()"
                (selectedChange)="skills.set($event)"
                labelledBy="user-access-skills-heading"
                describedBy="user-access-blurb"
                noun="skill"
                nounPlural="skills"
                emptyText="No skills in the catalog."
                maxHeight="sm"
              />
            }
          </div>

          <form [formGroup]="form" (ngSubmit)="save()" class="space-y-4">
            <div class="grid gap-4 md:grid-cols-2">
              <div>
                <label for="user-access-expires" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                  Expires
                </label>
                <input
                  id="user-access-expires"
                  type="datetime-local"
                  formControlName="expiresAt"
                  aria-describedby="user-access-expires-hint"
                  class="mt-1 block w-full rounded-sm border border-gray-300 px-3 py-2 text-sm/6 dark:border-gray-600 dark:bg-gray-900"
                />
                <p id="user-access-expires-hint" class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  Leave blank for a grant that does not lapse.
                </p>
              </div>
              <div>
                <label for="user-access-note" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                  Note
                </label>
                <input
                  id="user-access-note"
                  type="text"
                  formControlName="note"
                  maxlength="500"
                  placeholder="Why this user has this access"
                  class="mt-1 block w-full rounded-sm border border-gray-300 px-3 py-2 text-sm/6 dark:border-gray-600 dark:bg-gray-900"
                />
              </div>
            </div>

            <div class="flex flex-wrap items-center justify-between gap-4">
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                @if (hasGrant()) {
                  Granted by {{ grant().grantedBy || 'unknown' }} &middot; updated {{ formatDate(grant().updatedAt) }}
                } @else {
                  No direct grant. This user has only what their roles give.
                }
              </p>
              <div class="flex gap-3">
                @if (hasGrant()) {
                  <button
                    type="button"
                    (click)="remove()"
                    [disabled]="saving()"
                    class="px-4 py-2 text-sm/6 border border-gray-300 rounded-2xl hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:hover:bg-gray-700"
                  >
                    Remove grant
                  </button>
                }
                <button
                  type="submit"
                  [disabled]="!dirty() || saving()"
                  class="px-4 py-2 text-sm/6 bg-primary-accessible text-white rounded-2xl hover:brightness-95 disabled:opacity-50"
                >
                  {{ saving() ? 'Saving...' : 'Save' }}
                </button>
              </div>
            </div>
          </form>
        </div>
      }
    </section>
  `,
})
export class UserAccessSectionComponent implements OnInit {
  readonly userId = input.required<string>();

  private readonly http = inject(UserGrantHttpService);
  private readonly fb = inject(FormBuilder);
  private readonly dialog = inject(Dialog);
  private readonly toast = inject(ToastService);
  protected readonly toolService = inject(AdminToolService);
  protected readonly modelService = inject(ManagedModelsService);
  protected readonly skillService = inject(AdminSkillService);

  /** The stored grant as last loaded or saved; the baseline `dirty` compares against. */
  readonly grant = signal<UserGrant>(emptyGrant(''));
  readonly loading = signal(true);
  readonly saving = signal(false);
  readonly error = signal<string | null>(null);
  /** The backend surface is not mounted (flag off): the route 404s. */
  readonly unavailable = signal(false);

  readonly tools = signal<ReadonlySet<string>>(new Set());
  readonly models = signal<ReadonlySet<string>>(new Set());
  readonly skills = signal<ReadonlySet<string>>(new Set());

  readonly form = this.fb.group({
    expiresAt: this.fb.control('', { nonNullable: true }),
    note: this.fb.control('', { nonNullable: true }),
  });

  private readonly formValue = toSignal(this.form.valueChanges, { initialValue: this.form.value });

  readonly hasGrant = computed(() => {
    const g = this.grant();
    return g.grantedTools.length + g.grantedModels.length + g.grantedSkills.length > 0;
  });

  readonly dirty = computed(() => {
    const g = this.grant();
    const value = this.formValue();
    return (
      !sameList(g.grantedTools, this.tools()) ||
      !sameList(g.grantedModels, this.models()) ||
      !sameList(g.grantedSkills, this.skills()) ||
      localInputToIso(value.expiresAt ?? '') !== (g.expiresAt ? localInputToIso(isoToLocalInput(g.expiresAt)) : null) ||
      (value.note ?? '').trim() !== g.note
    );
  });

  readonly toolItems = computed<ToolSelectorItem[]>(() =>
    this.toolService.getTools().map((tool) => ({
      id: tool.toolId,
      name: tool.displayName,
      description: tool.description,
      group: tool.category,
      badge: isRetiring(tool) ? 'retiring' : undefined,
    })),
  );

  readonly modelItems = computed<ToolSelectorItem[]>(() =>
    this.modelService.getManagedModels().map((model) => ({
      id: model.modelId,
      name: model.modelName,
      description: model.modelId,
      group: model.providerName,
      badge: model.enabled ? undefined : 'disabled',
    })),
  );

  readonly skillItems = computed<ToolSelectorItem[]>(() =>
    this.skillService.getSkills().map((skill) => ({
      id: skill.skillId,
      name: skill.displayName,
      description: skill.description,
      group: skill.category ?? undefined,
      badge: skill.status === 'active' ? undefined : skill.status,
    })),
  );

  ngOnInit(): void {
    void this.load();
  }

  private async load(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const grant = await firstValueFrom(this.http.getGrant(this.userId()));
      this.adopt(grant);
    } catch (err) {
      if (err instanceof HttpErrorResponse && err.status === 404) {
        this.unavailable.set(true);
      } else {
        this.error.set(this.describe(err, 'Failed to load direct access.'));
      }
    } finally {
      this.loading.set(false);
    }
  }

  /** Make `grant` the baseline and reset the pickers and fields to it. */
  private adopt(grant: UserGrant): void {
    this.grant.set(grant);
    this.tools.set(new Set(grant.grantedTools));
    this.models.set(new Set(grant.grantedModels));
    this.skills.set(new Set(grant.grantedSkills));
    this.form.reset({ expiresAt: isoToLocalInput(grant.expiresAt), note: grant.note });
  }

  async save(): Promise<void> {
    if (!this.dirty() || this.saving()) return;
    this.saving.set(true);
    this.error.set(null);
    const value = this.form.getRawValue();
    const update: UserGrantUpdate = {
      grantedTools: [...this.tools()].sort(),
      grantedModels: [...this.models()].sort(),
      grantedSkills: [...this.skills()].sort(),
      expiresAt: localInputToIso(value.expiresAt),
      note: value.note.trim(),
    };
    try {
      const saved = await firstValueFrom(this.http.setGrant(this.userId(), update));
      this.adopt(saved);
      const count = update.grantedTools.length + update.grantedModels.length + update.grantedSkills.length;
      this.toast.success(count ? 'Direct access saved' : 'Direct grant removed');
    } catch (err) {
      this.error.set(this.describe(err, 'Failed to save direct access.'));
    } finally {
      this.saving.set(false);
    }
  }

  async remove(): Promise<void> {
    const ref = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: 'Remove direct grant?',
        message:
          'This user keeps everything their roles give and loses only what was granted here.',
        confirmText: 'Remove grant',
        destructive: true,
      } satisfies ConfirmationDialogData,
    });
    const confirmed = await firstValueFrom(ref.closed);
    if (!confirmed) return;

    this.saving.set(true);
    this.error.set(null);
    try {
      await firstValueFrom(this.http.deleteGrant(this.userId()));
      this.adopt(emptyGrant(this.userId()));
      this.toast.success('Direct grant removed');
    } catch (err) {
      this.error.set(this.describe(err, 'Failed to remove the direct grant.'));
    } finally {
      this.saving.set(false);
    }
  }

  formatDate(iso: string): string {
    if (!iso) return '—';
    const d = parseIso(iso);
    return isNaN(d.getTime()) ? '—' : d.toLocaleString();
  }

  private describe(err: unknown, fallback: string): string {
    if (err instanceof HttpErrorResponse) {
      const detail = err.error?.detail;
      if (typeof detail === 'string' && detail) return detail;
    }
    return fallback;
  }
}
