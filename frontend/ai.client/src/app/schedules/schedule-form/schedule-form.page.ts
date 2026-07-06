import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { FormBuilder, FormControl, ReactiveFormsModule, Validators } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowLeft } from '@ng-icons/heroicons/outline';
import { ScheduleService } from '../services/schedule.service';
import { AssistantService } from '../../assistants/services/assistant.service';
import { Assistant } from '../../assistants/models/assistant.model';
import { ToolService } from '../../services/tool/tool.service';
import { CreateScheduleRequest, ScheduleCadence, UpdateScheduleRequest } from '../models/schedule.model';
import { ToastService } from '../../services/toast/toast.service';

const WEEKDAY_LABELS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

/** All 24 hours, labeled 12-hour-with-suffix for the select. */
function hourOptions(): { value: number; label: string }[] {
  return Array.from({ length: 24 }, (_, hour) => ({
    value: hour,
    label:
      hour === 0
        ? '12:00 AM'
        : hour < 12
          ? `${hour}:00 AM`
          : hour === 12
            ? '12:00 PM'
            : `${hour - 12}:00 PM`,
  }));
}

/** IANA timezone names, via the runtime's own database when available. */
function timezoneOptions(): string[] {
  const intlWithSupport = Intl as typeof Intl & {
    supportedValuesOf?: (key: string) => string[];
  };
  if (typeof intlWithSupport.supportedValuesOf === 'function') {
    try {
      return intlWithSupport.supportedValuesOf('timeZone');
    } catch {
      // fall through to the static fallback below
    }
  }
  return [
    'America/Boise',
    'America/Denver',
    'America/Los_Angeles',
    'America/Chicago',
    'America/New_York',
    'America/Anchorage',
    'Pacific/Honolulu',
    'UTC',
  ];
}

/**
 * Create/edit page for a scheduled agent run. Bounded cadence UI only
 * (daily / weekday / weekly at an hour + IANA timezone) — no cron field,
 * per docs/specs/scheduled-agent-runs.md §3.
 *
 * Optional fields (assistant, tool selection) use explicit "clear"
 * checkboxes rather than relying on sending null: the backend's
 * `update_scheduled_prompt` reads a bare `null` as "leave unchanged", so a
 * clear is signalled with the explicit `clearAssistant` / `clearTools`
 * booleans instead. `clearAssistant` reverts to the default agent;
 * `clearTools` re-snapshots the caller's current RBAC-allowed tools. See
 * `buildUpdateRequest`.
 */
@Component({
  selector: 'app-schedule-form-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReactiveFormsModule, NgIcon, RouterLink],
  providers: [provideIcons({ heroArrowLeft })],
  templateUrl: './schedule-form.page.html',
})
export class ScheduleFormPage implements OnInit {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly fb = inject(FormBuilder);
  private readonly scheduleService = inject(ScheduleService);
  private readonly assistantService = inject(AssistantService);
  private readonly toolService = inject(ToolService);
  private readonly toast = inject(ToastService);

  readonly scheduleId = signal<string | null>(null);
  readonly mode = computed<'create' | 'edit'>(() => (this.scheduleId() ? 'edit' : 'create'));
  readonly saving = signal(false);
  readonly loadError = signal<string | null>(null);
  readonly loadingSchedule = signal(false);

  readonly assistants = this.assistantService.assistants$;
  readonly tools = this.toolService.tools;

  readonly hourOptions = hourOptions();
  readonly timezoneOptions = timezoneOptions();
  readonly weekdayLabels = WEEKDAY_LABELS;

  /**
   * Whether the assistant/tools sections should send a clear intent on
   * submit. Only meaningful in edit mode — create simply omits the field
   * when unchecked and nothing has been picked.
   */
  readonly clearAssistant = signal(false);
  readonly clearTools = signal(false);

  readonly selectedToolIds = signal<Set<string>>(new Set());

  readonly form = this.fb.group({
    label: ['', [Validators.required, Validators.maxLength(200)]],
    promptText: ['', [Validators.required, Validators.maxLength(20000)]],
    cadence: ['daily' as ScheduleCadence, [Validators.required]],
    hourLocal: [7, [Validators.required]],
    weekday: new FormControl<number | null>(null),
    timezone: [this.guessTimezone(), [Validators.required]],
    assistantId: new FormControl<string | null>(null),
  });

  readonly isWeekly = computed(() => this.form.controls.cadence.value === 'weekly');

  ngOnInit(): void {
    const id = this.route.snapshot.paramMap.get('scheduleId');
    this.scheduleId.set(id);

    void this.assistantService.loadAssistants(false, false);
    if (!this.toolService.initialized()) {
      void this.toolService.loadTools();
    }

    if (id) {
      void this.loadSchedule(id);
    }

    // "weekly" requires a weekday — default to Monday the first time it's chosen.
    this.form.controls.cadence.valueChanges.subscribe((cadence) => {
      if (cadence === 'weekly' && this.form.controls.weekday.value === null) {
        this.form.controls.weekday.setValue(1);
      }
    });
  }

  private async loadSchedule(id: string): Promise<void> {
    this.loadingSchedule.set(true);
    this.loadError.set(null);
    try {
      const schedule = await this.scheduleService.getSchedule(id);
      if (!schedule) {
        this.loadError.set('This schedule no longer exists.');
        return;
      }
      this.form.patchValue({
        label: schedule.label,
        promptText: schedule.promptText,
        cadence: schedule.cadence,
        hourLocal: schedule.hourLocal,
        weekday: schedule.weekday ?? null,
        timezone: schedule.timezone,
        assistantId: schedule.assistantId ?? null,
      });
      this.selectedToolIds.set(new Set(schedule.enabledTools ?? []));
    } catch {
      this.loadError.set('Failed to load this schedule.');
    } finally {
      this.loadingSchedule.set(false);
    }
  }

  private guessTimezone(): string {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    } catch {
      return 'UTC';
    }
  }

  assistantName(assistantId: string | null): string {
    if (!assistantId) return '';
    const match = (this.assistants() as Assistant[]).find((a) => a.assistantId === assistantId);
    return match?.name ?? assistantId;
  }

  toggleTool(toolId: string): void {
    this.selectedToolIds.update((set) => {
      const next = new Set(set);
      if (next.has(toolId)) {
        next.delete(toolId);
      } else {
        next.add(toolId);
      }
      return next;
    });
    this.clearTools.set(false);
  }

  isToolSelected(toolId: string): boolean {
    return this.selectedToolIds().has(toolId);
  }

  onClearAssistantChange(checked: boolean): void {
    this.clearAssistant.set(checked);
    if (checked) {
      this.form.controls.assistantId.setValue(null);
    }
  }

  onClearToolsChange(checked: boolean): void {
    this.clearTools.set(checked);
    if (checked) {
      this.selectedToolIds.set(new Set());
    }
  }

  getFieldError(fieldName: 'label' | 'promptText'): string | null {
    const field = this.form.get(fieldName);
    if (!field || !field.touched || !field.errors) {
      return null;
    }
    if (field.errors['required']) return 'This field is required';
    if (field.errors['maxlength']) {
      return `Maximum length is ${field.errors['maxlength'].requiredLength} characters`;
    }
    return null;
  }

  async onSubmit(): Promise<void> {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }
    const value = this.form.getRawValue();
    if (value.cadence === 'weekly' && value.weekday === null) {
      this.form.controls.weekday.setErrors({ required: true });
      return;
    }

    this.saving.set(true);
    try {
      const toolIds = Array.from(this.selectedToolIds());
      if (this.mode() === 'create') {
        const request: CreateScheduleRequest = {
          label: value.label!,
          promptText: value.promptText!,
          cadence: value.cadence!,
          hourLocal: value.hourLocal!,
          weekday: value.cadence === 'weekly' ? value.weekday : null,
          timezone: value.timezone!,
          assistantId: value.assistantId ?? null,
          enabledTools: toolIds.length > 0 ? toolIds : null,
        };
        await this.scheduleService.createSchedule(request);
        this.toast.success('Schedule created', `"${request.label}" will run on the cadence you set.`);
      } else {
        const request = this.buildUpdateRequest(value, toolIds);
        await this.scheduleService.updateSchedule(this.scheduleId()!, request);
        this.toast.success('Schedule updated');
      }
      this.router.navigate(['/schedules']);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to save the schedule';
      this.toast.error('Could not save schedule', message);
    } finally {
      this.saving.set(false);
    }
  }

  /**
   * Builds the PATCH body for edit mode. `assistantId` and `enabledTools`
   * cannot be cleared with a bare null (the backend reads null as "leave
   * unchanged"), so the "clear" checkboxes send an explicit `clearAssistant`
   * / `clearTools` intent instead. A clear flag and a value are mutually
   * exclusive (the backend rejects both), so each pair is either a clear, a
   * set, or omitted.
   */
  private buildUpdateRequest(
    value: ReturnType<ScheduleFormPage['form']['getRawValue']>,
    toolIds: string[],
  ): UpdateScheduleRequest {
    const request: UpdateScheduleRequest = {
      label: value.label!,
      promptText: value.promptText!,
      cadence: value.cadence!,
      hourLocal: value.hourLocal!,
      weekday: value.cadence === 'weekly' ? value.weekday : null,
      timezone: value.timezone!,
    };
    if (this.clearAssistant()) {
      request.clearAssistant = true;
    } else if (value.assistantId) {
      request.assistantId = value.assistantId;
    }
    if (this.clearTools()) {
      request.clearTools = true;
    } else if (toolIds.length > 0) {
      request.enabledTools = toolIds;
    }
    return request;
  }

  onCancel(): void {
    this.router.navigate(['/schedules']);
  }
}
