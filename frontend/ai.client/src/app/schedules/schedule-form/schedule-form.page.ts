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
import { RunNowService } from '../services/run-now.service';
import { AgentService } from '../../agents/services/agent.service';
import { Agent } from '../../agents/models/agent.model';
import { ToolService } from '../../services/tool/tool.service';
import {
  CreateScheduleRequest,
  IntervalUnit,
  MIN_INTERVAL_MINUTES,
  RunNowRequest,
  ScheduleCadence,
  UpdateScheduleRequest,
} from '../models/schedule.model';
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
 * The "target" is an Agent (the Agent Designer primitive that supersedes the
 * Assistant — same underlying record, `agentId == assistantId`, so the wire
 * field stays `assistantId` for backend compatibility). When an Agent is
 * selected, its **bound tools govern the run**: the inference path replaces the
 * request's `enabled_tools` with the Agent's `tool` bindings at invocation
 * (`agent_binding_resolver` / routes.py `effective_enabled_tools`). So the
 * manual tool picker is only shown for the "Default agent" case — selecting an
 * Agent hides it, and any prior snapshot is dropped so it can't shadow the
 * Agent's own toolset.
 *
 * Optional fields (agent, tool selection) use explicit "clear" checkboxes
 * rather than relying on sending null: the backend's `update_scheduled_prompt`
 * reads a bare `null` as "leave unchanged", so a clear is signalled with the
 * explicit `clearAssistant` / `clearTools` booleans instead. `clearAssistant`
 * reverts to the default agent; `clearTools` re-snapshots the caller's current
 * RBAC-allowed tools. See `buildUpdateRequest`.
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
  private readonly runNowService = inject(RunNowService);
  private readonly agentService = inject(AgentService);
  private readonly toolService = inject(ToolService);
  private readonly toast = inject(ToastService);

  readonly scheduleId = signal<string | null>(null);
  readonly mode = computed<'create' | 'edit'>(() => (this.scheduleId() ? 'edit' : 'create'));
  readonly saving = signal(false);
  readonly loadError = signal<string | null>(null);
  readonly loadingSchedule = signal(false);

  readonly minIntervalMinutes = MIN_INTERVAL_MINUTES;
  readonly intervalUnitOptions: IntervalUnit[] = ['minutes', 'hours'];

  readonly agents = this.agentService.agents$;
  readonly tools = this.toolService.tools;

  readonly hourOptions = hourOptions();
  readonly timezoneOptions = timezoneOptions();
  readonly weekdayLabels = WEEKDAY_LABELS;

  /**
   * Whether the agent/tools sections should send a clear intent on submit.
   * Only meaningful in edit mode — create simply omits the field when
   * unchecked and nothing has been picked.
   */
  readonly clearAssistant = signal(false);
  readonly clearTools = signal(false);

  readonly selectedToolIds = signal<Set<string>>(new Set());

  /**
   * Mirror of the `assistantId` control value (form values aren't signals) so
   * the template can reactively hide the manual tool picker when an Agent is
   * chosen. A selected Agent's bound tools replace the run's `enabled_tools`
   * server-side, so a manual picker there would be silently discarded.
   */
  readonly selectedAgentId = signal<string | null>(null);
  readonly agentSelected = computed(() => !!this.selectedAgentId());
  readonly selectedAgentName = computed<string>(() => {
    const id = this.selectedAgentId();
    if (!id) return '';
    return (this.agents() as Agent[]).find((a) => a.agentId === id)?.name ?? id;
  });

  readonly form = this.fb.group({
    label: ['', [Validators.required, Validators.maxLength(200)]],
    promptText: ['', [Validators.required, Validators.maxLength(20000)]],
    cadence: ['daily' as ScheduleCadence, [Validators.required]],
    hourLocal: [7, [Validators.required]],
    weekday: new FormControl<number | null>(null),
    intervalValue: new FormControl<number | null>(null),
    intervalUnit: ['hours' as IntervalUnit, [Validators.required]],
    timezone: [this.guessTimezone(), [Validators.required]],
    assistantId: new FormControl<string | null>(null),
  });

  private readonly cadenceValue = signal<ScheduleCadence>('daily');
  readonly isWeekly = computed(() => this.cadenceValue() === 'weekly');
  readonly isInterval = computed(() => this.cadenceValue() === 'interval');

  ngOnInit(): void {
    const id = this.route.snapshot.paramMap.get('scheduleId');
    this.scheduleId.set(id);

    void this.agentService.loadAgents(false);
    if (!this.toolService.initialized()) {
      void this.toolService.loadTools();
    }

    if (id) {
      void this.loadSchedule(id);
    }

    // Keep the selected-agent signal in sync with the control so the tool
    // picker hides/shows reactively.
    this.selectedAgentId.set(this.form.controls.assistantId.value ?? null);
    this.form.controls.assistantId.valueChanges.subscribe((value) => {
      this.selectedAgentId.set(value ?? null);
    });

    // Keep the cadence-driven signals in sync (form values aren't signals, so
    // the isWeekly/isInterval computeds read this instead of the control).
    this.cadenceValue.set(this.form.controls.cadence.value ?? 'daily');
    this.form.controls.cadence.valueChanges.subscribe((cadence) => {
      this.cadenceValue.set(cadence ?? 'daily');
      // "weekly" requires a weekday — default to Monday the first time it's chosen.
      if (cadence === 'weekly' && this.form.controls.weekday.value === null) {
        this.form.controls.weekday.setValue(1);
      }
      // "interval" needs a magnitude — default to every 6 hours the first time.
      if (cadence === 'interval' && this.form.controls.intervalValue.value === null) {
        this.form.controls.intervalValue.setValue(6);
        this.form.controls.intervalUnit.setValue('hours');
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
        intervalValue: schedule.intervalValue ?? null,
        intervalUnit: schedule.intervalUnit ?? 'hours',
        timezone: schedule.timezone,
        assistantId: schedule.assistantId ?? null,
      });
      this.cadenceValue.set(schedule.cadence);
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
    if (value.cadence === 'interval' && !this.isIntervalValid(value.intervalValue, value.intervalUnit)) {
      this.form.controls.intervalValue.setErrors({ min: true });
      this.form.controls.intervalValue.markAsTouched();
      return;
    }

    this.saving.set(true);
    try {
      // A selected Agent owns its toolset — its `tool` bindings replace the
      // run's `enabled_tools` server-side — so never send a manual snapshot
      // alongside one; it would be dead data. The picker is hidden in that
      // case, but guard here too so a stale selection can't leak through.
      const toolIds = value.assistantId ? [] : Array.from(this.selectedToolIds());
      if (this.mode() === 'create') {
        const request: CreateScheduleRequest = {
          label: value.label!,
          promptText: value.promptText!,
          cadence: value.cadence!,
          hourLocal: value.hourLocal!,
          weekday: value.cadence === 'weekly' ? value.weekday : null,
          intervalValue: value.cadence === 'interval' ? value.intervalValue : null,
          intervalUnit: value.cadence === 'interval' ? value.intervalUnit : null,
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
      intervalValue: value.cadence === 'interval' ? value.intervalValue : null,
      intervalUnit: value.cadence === 'interval' ? value.intervalUnit : null,
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

  /** True when value+unit clear the backend's minimum-interval floor. */
  private isIntervalValid(value: number | null, unit: IntervalUnit | null): boolean {
    if (value === null || value < 1 || unit === null) {
      return false;
    }
    const minutes = unit === 'hours' ? value * 60 : value;
    return minutes >= this.minIntervalMinutes;
  }

  /**
   * Fire the prompt once immediately via POST /runs/now — the attended test
   * surface. Does NOT save a schedule and does NOT change the view: the run is
   * handed to `RunNowService`, which tracks it as a persistent background-task
   * toast and, when it finishes, links to the session-detail conversation
   * where the result renders with full formatting. Only the prompt is required.
   */
  runNow(): void {
    const promptText = this.form.controls.promptText.value?.trim();
    if (!promptText) {
      this.form.controls.promptText.setErrors({ required: true });
      this.form.controls.promptText.markAsTouched();
      return;
    }

    const agentId = this.form.controls.assistantId.value ?? null;
    // Mirror the schedule's targeting: run against the selected Agent (whose
    // bound tools govern the turn), else fall back to the manual tool snapshot
    // for the default agent.
    const toolIds = agentId ? [] : Array.from(this.selectedToolIds());
    const request: RunNowRequest = {
      prompt: promptText,
      title: this.form.controls.label.value?.trim() || null,
      ragAssistantId: agentId,
      enabledTools: toolIds.length > 0 ? toolIds : null,
    };
    this.runNowService.run(request);
  }

  onCancel(): void {
    this.router.navigate(['/schedules']);
  }
}
