import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  signal,
} from '@angular/core';
import { DatePipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationTriangle, heroInformationCircle } from '@ng-icons/heroicons/outline';
import { TaskModelOption, TaskModelSlot } from '../models/task-model.model';
import { effortLevelLabel } from '../models/managed-model.model';
import { TaskModelsService } from '../services/task-models.service';

const PRICE = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 3,
});

/** Value of the model picker's "run the default" option. */
const DEFAULT_CHOICE = '';

/**
 * One task on the Task models tab: what it does, what it runs on now, and a model + effort
 * picker. The choices come from the backend (`eligibleModels`); nothing here decides
 * eligibility.
 */
@Component({
  selector: 'app-task-model-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, DatePipe],
  providers: [provideIcons({ heroExclamationTriangle, heroInformationCircle })],
  host: { class: 'block' },
  templateUrl: './task-model-card.component.html',
})
export class TaskModelCardComponent {
  private readonly service = inject(TaskModelsService);

  readonly task = input.required<TaskModelSlot>();

  /** The picker's current selection: a model id, or '' for the default. */
  protected readonly draftModelId = signal(DEFAULT_CHOICE);
  /** The picker's current effort: a level, or '' for the model's own default. */
  protected readonly draftEffort = signal('');
  protected readonly saving = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly savedNotice = signal(false);

  protected readonly effortLabel = effortLevelLabel;

  constructor() {
    // Reset the draft whenever the stored task changes (first load, after a save).
    effect(() => {
      const task = this.task();
      this.draftModelId.set(task.assignment?.modelId ?? DEFAULT_CHOICE);
      this.draftEffort.set(task.assignment?.effort ?? '');
    });
  }

  protected readonly ids = computed(() => {
    const slot = this.task().slot;
    return {
      model: `task-model-${slot}`,
      effort: `task-effort-${slot}`,
      help: `task-help-${slot}`,
    };
  });

  protected readonly defaultLabel = computed(() => {
    const model = this.task().defaultModel;
    if (!model) {
      return 'Default (not configured)';
    }
    return `Default: ${model.modelName ?? model.modelId}`;
  });

  /** The eligible models, plus the stored assignment if it is no longer offered. */
  protected readonly options = computed<TaskModelOption[]>(() => {
    const task = this.task();
    const options = [...task.eligibleModels];
    const assigned = task.assignment;
    if (assigned && !options.some(o => o.modelId === assigned.modelId)) {
      options.unshift({
        modelId: assigned.modelId,
        modelName: `${assigned.modelName ?? assigned.modelId} (no longer available)`,
        providerName: '',
        effortLevels: [],
        effortDefault: null,
        inputPricePerMillionTokens: 0,
        outputPricePerMillionTokens: 0,
      });
    }
    return options;
  });

  protected readonly selectedOption = computed(() =>
    this.options().find(o => o.modelId === this.draftModelId()) ?? null,
  );

  protected readonly effortLevels = computed(() => this.selectedOption()?.effortLevels ?? []);

  protected readonly effortDefaultLabel = computed(() => {
    const level = this.selectedOption()?.effortDefault;
    return level ? `Model default (${effortLevelLabel(level)})` : 'Model default';
  });

  protected readonly dirty = computed(() => {
    const task = this.task();
    return (
      this.draftModelId() !== (task.assignment?.modelId ?? DEFAULT_CHOICE) ||
      this.draftEffort() !== (task.assignment?.effort ?? '')
    );
  });

  protected readonly runsOn = computed(() => {
    const effective = this.task().effective;
    return effective.modelName || effective.modelId || 'no model (not configured)';
  });

  protected formatPrice(option: TaskModelOption): string {
    if (!option.providerName) {
      return '';
    }
    const input = PRICE.format(option.inputPricePerMillionTokens);
    const output = PRICE.format(option.outputPricePerMillionTokens);
    return ` · ${input} in / ${output} out per MTok`;
  }

  protected onModelChange(event: Event): void {
    const value = (event.target as HTMLSelectElement).value;
    this.draftModelId.set(value);
    // An effort level belongs to a model; a different model starts at its own default.
    const levels = this.options().find(o => o.modelId === value)?.effortLevels ?? [];
    if (!levels.includes(this.draftEffort())) {
      this.draftEffort.set('');
    }
    this.error.set(null);
    this.savedNotice.set(false);
  }

  protected onEffortChange(event: Event): void {
    this.draftEffort.set((event.target as HTMLSelectElement).value);
    this.error.set(null);
    this.savedNotice.set(false);
  }

  protected discard(): void {
    const task = this.task();
    this.draftModelId.set(task.assignment?.modelId ?? DEFAULT_CHOICE);
    this.draftEffort.set(task.assignment?.effort ?? '');
    this.error.set(null);
  }

  protected async save(): Promise<void> {
    if (this.saving() || !this.dirty()) {
      return;
    }
    this.saving.set(true);
    this.error.set(null);
    this.savedNotice.set(false);
    const modelId = this.draftModelId() || null;
    try {
      await this.service.update(this.task().slot, {
        modelId,
        effort: modelId ? this.draftEffort() || null : null,
      });
      this.savedNotice.set(true);
    } catch (err) {
      const detail = err instanceof HttpErrorResponse ? err.error?.detail : null;
      this.error.set(typeof detail === 'string' ? detail : 'Could not save. Please try again.');
    } finally {
      this.saving.set(false);
    }
  }
}
