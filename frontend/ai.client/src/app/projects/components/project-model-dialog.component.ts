import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroCheck } from '@ng-icons/heroicons/outline';
import { BindableItem } from '../../agents/models/agent.model';
import { DialogShellComponent } from '../../components/dialog/dialog-shell.component';
import { ToastService } from '../../services/toast/toast.service';
import { ModelConfig, ModelResponse } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';

export interface ProjectModelDialogData {
  projectId: string;
  /** The project's model, or null for each member's own default. */
  modelId: string | null;
  /** The models the caller may bind (`AgentService.loadBindable('model')`). */
  models: BindableItem[];
  canEdit: boolean;
}

/** The saved model, or `undefined` when nothing was saved. */
export type ProjectModelDialogResult = ModelResponse | undefined;

/**
 * Pick the model every task in the project runs on. A member who can't use it
 * gets their own default instead (shared-projects §9.6), which the dialog says.
 */
@Component({
  selector: 'app-project-model-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, NgIcon],
  providers: [provideIcons({ heroCheck })],
  template: `
    <app-dialog-shell
      title="Model"
      description="The model every task in this project runs on. Members who can’t use it get their own default instead."
      (closed)="cancel()"
    >
      @if (data.models.length === 0) {
        <p class="text-sm/6 text-gray-600 dark:text-gray-400">No models are available to you.</p>
      } @else {
        <ul class="divide-y divide-gray-200 overflow-hidden rounded-2xl border border-gray-200 dark:divide-gray-700 dark:border-gray-700" role="radiogroup" aria-label="Model">
          @for (m of data.models; track m.ref) {
            <li>
              <button
                type="button"
                role="radio"
                [attr.aria-label]="m.label"
                [attr.aria-checked]="selected() === m.ref"
                [attr.cdkFocusInitial]="selected() === m.ref ? '' : null"
                [disabled]="!data.canEdit"
                (click)="selected.set(m.ref)"
                class="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-gray-50 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-default disabled:hover:bg-transparent aria-checked:bg-gray-50 dark:hover:bg-gray-700/50 dark:aria-checked:bg-gray-700/50"
              >
                <span class="min-w-0 flex-1">
                  <span class="block text-sm/6 font-medium text-gray-900 dark:text-white">{{ m.label }}</span>
                  @if (m.description) {
                    <span class="block text-xs/5 text-gray-600 dark:text-gray-400">{{ m.description }}</span>
                  }
                </span>
                @if (selected() === m.ref) {
                  <ng-icon name="heroCheck" class="mt-1 size-4 shrink-0 text-primary-accessible dark:text-primary-50" aria-hidden="true" />
                }
              </button>
            </li>
          }
        </ul>
        @if (data.modelId && !known()) {
          <p class="mt-3 text-xs/5 text-gray-600 dark:text-gray-400">
            The project is set to <span class="font-medium">{{ data.modelId }}</span>, which isn’t a model you can use. Choosing another replaces it.
          </p>
        }
      }
      @if (error()) {
        <p role="alert" class="mt-3 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ error() }}</p>
      }

      <div dialogFooter class="flex justify-end gap-3 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
        <button
          type="button"
          (click)="cancel()"
          class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white"
        >
          {{ data.canEdit ? 'Cancel' : 'Close' }}
        </button>
        @if (data.canEdit) {
          <button
            type="button"
            (click)="save()"
            [disabled]="!dirty() || saving()"
            class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {{ saving() ? 'Saving…' : 'Save model' }}
          </button>
        }
      </div>
    </app-dialog-shell>
  `,
})
export class ProjectModelDialogComponent {
  private dialogRef = inject<DialogRef<ProjectModelDialogResult>>(DialogRef);
  private api = inject(ProjectApiService);
  private toast = inject(ToastService);

  protected readonly data = inject<ProjectModelDialogData>(DIALOG_DATA);
  protected readonly selected = signal<string | null>(this.data.modelId);
  protected readonly saving = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly known = computed(() => this.data.models.some(m => m.ref === this.data.modelId));
  protected readonly dirty = computed(() => this.selected() !== null && this.selected() !== this.data.modelId);

  protected async save(): Promise<void> {
    const modelId = this.selected();
    if (!modelId || this.saving()) return;
    const provider = this.data.models.find(m => m.ref === modelId)?.meta?.['provider'] as string | undefined;
    const config: ModelConfig = { modelId, ...(provider ? { provider } : {}) };
    this.saving.set(true);
    this.error.set(null);
    try {
      const response = await firstValueFrom(this.api.saveModel(this.data.projectId, config));
      this.toast.success('Model saved');
      this.dialogRef.close(response);
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'The model could not be saved.'));
    } finally {
      this.saving.set(false);
    }
  }

  protected cancel(): void {
    this.dialogRef.close(undefined);
  }
}
