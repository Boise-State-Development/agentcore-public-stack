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
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowLeft } from '@ng-icons/heroicons/outline';
import { ScheduleService } from '../services/schedule.service';
import { RunNowService } from '../services/run-now.service';
import { AgentService } from '../../agents/services/agent.service';
import { Agent } from '../../agents/models/agent.model';
import { Tool, ToolService, isRetiring, retirementDetail } from '../../services/tool/tool.service';
import {
  CreateScheduleRequest,
  IntervalUnit,
  MIN_INTERVAL_MINUTES,
  RunNowRequest,
  ScheduleCadence,
  ScheduleMode,
  UpdateScheduleRequest,
} from '../models/schedule.model';
import { FEATURES } from '../../services/features';
import { ModelService } from '../../session/services/model/model.service';
import { isRetiring as isModelRetiring } from '../../shared/utils/retirement';
import { ToastService } from '../../services/toast/toast.service';
import { ToolSelectorComponent } from '../../components/tool-selector/tool-selector.component';
import { ToolSelectorItem } from '../../components/tool-selector/tool-selector.model';
import { ProjectApiService } from '../../projects/services/project-api.service';
import { projectErrorMessage } from '../../projects/services/projects.service';
import {
  CreateProjectScheduleRequest,
  Project,
  ProjectSchedule,
  UpdateProjectScheduleRequest,
} from '../../projects/models/project.model';
import { WEEKDAY_LABELS, runsPerDay } from '../schedule-wording';

/** A watcher this often or more gets the "use a small model" note (quiet-watcher §3.1). */
const WATCH_NOTE_UNDER_MINUTES = 60;

/** One row of the schedule's Model picker. */
export interface ScheduleModelChoice {
  modelId: string;
  label: string;
}

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
 *
 * **Project mode** (`?projectId=…`, shared-projects 3.2c): the schedule runs on
 * that project's assistant with the project's tools, so the agent and tool
 * pickers give way to where its runs go ("Share each run with the project",
 * `deliverTo`) and whether members hear about them (`notifyMembers`). It saves
 * through `/projects/{id}/schedules` and returns to the project with its
 * Schedules dialog open. Only the creator edits one (the server says so with
 * `canEdit`), and an edit sends only the fields that changed, so the project's
 * Activity names what actually moved.
 */
@Component({
  selector: 'app-schedule-form-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReactiveFormsModule, NgIcon, RouterLink, ToolSelectorComponent],
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
  private readonly projectApi = inject(ProjectApiService);
  protected readonly features = inject(FEATURES);
  /**
   * The chat composer's model list (`/models`, already filtered to what this user
   * can use). Injected only while quiet watchers are on, so a build with them off
   * makes no request it didn't make before.
   */
  private readonly modelService = this.features.proactiveRuns ? inject(ModelService) : null;

  readonly scheduleId = signal<string | null>(null);
  /** Set in project mode: the schedule runs on this project's assistant. */
  readonly projectId = signal<string | null>(null);
  readonly project = signal<Project | null>(null);
  readonly isProject = computed(() => !!this.projectId());
  /** The project schedule as loaded, so an edit sends only what changed. */
  private loadedProjectSchedule: ProjectSchedule | null = null;
  /** Mirror of the share checkbox, so "notify members" can follow it. */
  readonly shareWithProject = signal(false);
  /** Where Back and Cancel go. */
  readonly backLink = computed<string[]>(() => {
    const id = this.projectId();
    return id ? ['/projects', id, 'schedules'] : ['/schedules'];
  });
  readonly backLabel = computed(() => {
    if (!this.isProject()) return 'Back to schedules';
    const name = this.project()?.name;
    return name ? `Back to ${name}` : 'Back to the project';
  });
  readonly mode = computed<'create' | 'edit'>(() => (this.scheduleId() ? 'edit' : 'create'));
  readonly saving = signal(false);
  /** A Run now from this tab hasn't finished; the button waits for it. */
  readonly runInFlight = this.runNowService.running;
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

  readonly selectedToolIds = signal<ReadonlySet<string>>(new Set());

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
    deliverToProject: [false],
    notifyMembers: new FormControl<boolean>({ value: false, disabled: true }, { nonNullable: true }),
    publishOutputs: new FormControl<boolean>({ value: false, disabled: true }, { nonNullable: true }),
    // Quiet-watcher fields (quiet-watcher §3.1): rendered and sent only while
    // `features.proactiveRuns` is on, and only for a personal schedule.
    mode: new FormControl<ScheduleMode>('standard', { nonNullable: true }),
    modelId: new FormControl<string | null>(null),
  });

  private readonly cadenceValue = signal<ScheduleCadence>('daily');
  readonly isWeekly = computed(() => this.cadenceValue() === 'weekly');
  readonly isInterval = computed(() => this.cadenceValue() === 'interval');

  /** Whether this form offers the mode and model fields: the flag, and a personal schedule. */
  readonly offersWatchFields = computed(() => this.features.proactiveRuns && !this.isProject());
  /** Mirrors of the mode, model and interval controls, for the template's computeds. */
  readonly modeValue = signal<ScheduleMode>('standard');
  readonly modelIdValue = signal<string | null>(null);
  private readonly intervalMinutes = signal<number | null>(null);
  /** The model the schedule had when it was loaded, so an edit sends only a change. */
  private loadedModelId: string | null = null;

  /**
   * The Model picker's rows: the composer's list, less models being retired (their
   * successor is the one to choose), but always the schedule's own model, even one
   * this list no longer has, so the select never silently shows "Default".
   */
  readonly modelChoices = computed<ScheduleModelChoice[]>(() => {
    const service = this.modelService;
    if (!service) return [];
    const current = this.modelIdValue();
    const choices = service
      .availableModels()
      .filter((model) => !isModelRetiring(model) || model.modelId === current)
      .map((model) => ({
        modelId: model.modelId,
        label: model.providerName ? `${model.modelName} (${model.providerName})` : model.modelName,
      }));
    if (current && !choices.some((choice) => choice.modelId === current)) {
      choices.unshift({ modelId: current, label: service.modelNameFor(current) ?? current });
    }
    return choices;
  });
  readonly modelsLoading = computed(() => this.modelService?.modelsLoading() ?? false);

  /**
   * The note for a watcher that checks more often than hourly: how many runs a day
   * that is, and to choose a small, inexpensive model. The catalog has no "small"
   * marker, so it names none. Null when it doesn't apply.
   */
  readonly shortIntervalNote = computed<string | null>(() => {
    if (!this.offersWatchFields() || this.modeValue() !== 'watch' || !this.isInterval()) return null;
    const minutes = this.intervalMinutes();
    if (minutes === null || minutes < MIN_INTERVAL_MINUTES || minutes >= WATCH_NOTE_UNDER_MINUTES) return null;
    return (
      `This watcher checks about ${runsPerDay(minutes)} times a day. Each check is a full run, ` +
      'so choose a small, inexpensive model under Model to keep it cheap.'
    );
  });

  ngOnInit(): void {
    const id = this.route.snapshot.paramMap.get('scheduleId');
    this.scheduleId.set(id);
    const projectId = this.route.snapshot.queryParamMap.get('projectId');
    this.projectId.set(projectId);

    this.form.controls.deliverToProject.valueChanges.subscribe((share) => this.onShareChange(!!share));

    if (projectId) {
      // The project's assistant and its tools decide; there is nothing to pick.
      void this.loadProject(projectId, id);
    } else {
      void this.agentService.loadAgents(false);
      if (!this.toolService.initialized()) {
        void this.toolService.loadTools();
      }
      if (id) {
        void this.loadSchedule(id);
      }
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
      // "weekly" requires a weekday — default to Monday (0) the first time it's chosen.
      if (cadence === 'weekly' && this.form.controls.weekday.value === null) {
        this.form.controls.weekday.setValue(0);
      }
      // "interval" needs a magnitude — default to every 6 hours the first time.
      if (cadence === 'interval' && this.form.controls.intervalValue.value === null) {
        this.form.controls.intervalValue.setValue(6);
        this.form.controls.intervalUnit.setValue('hours');
      }
    });

    this.form.controls.mode.valueChanges.subscribe((mode) => this.modeValue.set(mode ?? 'standard'));
    this.form.controls.modelId.valueChanges.subscribe((modelId) => this.modelIdValue.set(modelId || null));
    this.syncIntervalMinutes();
    this.form.controls.intervalValue.valueChanges.subscribe(() => this.syncIntervalMinutes());
    this.form.controls.intervalUnit.valueChanges.subscribe(() => this.syncIntervalMinutes());
  }

  private syncIntervalMinutes(): void {
    const value = Number(this.form.controls.intervalValue.value);
    const unit = this.form.controls.intervalUnit.value;
    this.intervalMinutes.set(
      Number.isFinite(value) && value > 0 && unit ? (unit === 'hours' ? value * 60 : value) : null,
    );
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
        mode: schedule.mode ?? 'standard',
        modelId: schedule.modelId ?? null,
      });
      this.cadenceValue.set(schedule.cadence);
      this.modeValue.set(schedule.mode ?? 'standard');
      this.modelIdValue.set(schedule.modelId ?? null);
      this.loadedModelId = schedule.modelId ?? null;
      this.syncIntervalMinutes();
      this.selectedToolIds.set(new Set(schedule.enabledTools ?? []));
    } catch {
      this.loadError.set('Failed to load this schedule.');
    } finally {
      this.loadingSchedule.set(false);
    }
  }

  /** Project mode: the project (for its name and assistant) and, to edit, its schedule. */
  private async loadProject(projectId: string, scheduleId: string | null): Promise<void> {
    this.loadingSchedule.set(true);
    this.loadError.set(null);
    try {
      const [project, schedule] = await Promise.all([
        firstValueFrom(this.projectApi.get(projectId)),
        scheduleId ? firstValueFrom(this.projectApi.schedule(projectId, scheduleId)) : Promise.resolve(null),
      ]);
      this.project.set(project);
      if (schedule && !schedule.canEdit) {
        this.loadError.set(
          schedule.isMine
            ? 'This project is archived, so its schedules can’t be changed.'
            : 'Only the person who made this schedule can change it.',
        );
        return;
      }
      if (!schedule && (project.role === 'viewer' || project.status !== 'active')) {
        this.loadError.set(
          project.status !== 'active'
            ? 'This project is archived, so it can’t get new schedules.'
            : 'Only editors and the owner can add schedules to this project.',
        );
        return;
      }
      if (schedule) {
        this.loadedProjectSchedule = schedule;
        this.form.patchValue({
          label: schedule.label,
          promptText: schedule.promptText,
          cadence: schedule.cadence,
          hourLocal: schedule.hourLocal,
          weekday: schedule.weekday ?? null,
          intervalValue: schedule.intervalValue ?? null,
          intervalUnit: schedule.intervalUnit ?? 'hours',
          timezone: schedule.timezone,
          deliverToProject: schedule.deliverTo === 'project',
        });
        this.form.controls.notifyMembers.setValue(schedule.notifyMembers);
        this.form.controls.publishOutputs.setValue(schedule.publishOutputs);
        this.cadenceValue.set(schedule.cadence);
      }
    } catch (err) {
      this.loadError.set(projectErrorMessage(err, scheduleId ? 'Failed to load this schedule.' : 'Failed to load this project.'));
    } finally {
      this.loadingSchedule.set(false);
    }
  }

  /** "Notify members" and "Publish to Outputs" only mean something while runs are shared. */
  private onShareChange(share: boolean): void {
    this.shareWithProject.set(share);
    for (const control of [this.form.controls.notifyMembers, this.form.controls.publishOutputs]) {
      if (share) {
        control.enable({ emitEvent: false });
      } else {
        control.setValue(false, { emitEvent: false });
        control.disable({ emitEvent: false });
      }
    }
  }

  private guessTimezone(): string {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    } catch {
      return 'UTC';
    }
  }

  /**
   * A schedule's `enabledTools` is a snapshot, so adding a retiring tool here
   * books it into a run that may fire months from now — new adoption by another
   * door. Turning one off stays open, same asymmetry as the other two pickers
   * (docs/specs/mcp-server-retirement.md §7).
   *
   * Only ever a *narrowing* of what the checkbox offers: the backend re-filters
   * this list against the caller's grant at run time regardless.
   */
  isToolRetiring(toolId: string): boolean {
    const tool = this.tools().find((t) => t.toolId === toolId);
    return !!tool && isRetiring(tool) && !this.isToolSelected(toolId);
  }

  /** The picker's rows. A retiring tool says so in visible text, not a hover title. */
  readonly toolItems = computed<ToolSelectorItem[]>(() =>
    this.tools().map((tool) => {
      const retiring = isRetiring(tool);
      return {
        id: tool.toolId,
        name: tool.displayName,
        description: tool.description,
        group: tool.category,
        badge: retiring ? 'retiring' : undefined,
        note: retiring ? this.retiringNote(tool) : undefined,
        addBlocked: retiring,
      };
    }),
  );

  /** The retiring row's line: why it can't be added (or should go), then what to use instead. */
  retiringNote(tool: Tool): string {
    const lead = this.isToolSelected(tool.toolId)
      ? 'Being retired — remove it from this schedule when you can.'
      : 'Being retired and can no longer be added to a schedule.';
    const detail = retirementDetail(tool);
    return detail ? `${lead} ${detail}` : lead;
  }

  /**
   * The picker's new selection. It already refuses to add a retiring tool; this is
   * the backstop behind it, so no path can book one into a snapshot.
   */
  onToolSelectionChange(next: ReadonlySet<string>): void {
    const allowed = new Set([...next].filter((toolId) => !this.isToolRetiring(toolId)));
    this.selectedToolIds.set(allowed);
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

    if (this.isProject()) {
      await this.saveProjectSchedule(value);
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
        if (this.offersWatchFields()) {
          // Only while the flag is on: with it off the backend refuses these (422),
          // and the body stays exactly what it was before watchers existed.
          request.mode = value.mode;
          if (value.modelId) request.modelId = value.modelId;
        }
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

  /** Project mode's save: the project's routes, then back to its Schedules dialog. */
  private async saveProjectSchedule(value: ReturnType<ScheduleFormPage['form']['getRawValue']>): Promise<void> {
    const projectId = this.projectId()!;
    const request: CreateProjectScheduleRequest = {
      label: value.label!,
      promptText: value.promptText!,
      cadence: value.cadence!,
      hourLocal: Number(value.hourLocal),
      weekday: value.cadence === 'weekly' ? Number(value.weekday) : null,
      intervalValue: value.cadence === 'interval' ? value.intervalValue : null,
      intervalUnit: value.cadence === 'interval' ? value.intervalUnit : null,
      timezone: value.timezone!,
      deliverTo: value.deliverToProject ? 'project' : 'owner',
      notifyMembers: !!value.deliverToProject && !!value.notifyMembers,
      publishOutputs: !!value.deliverToProject && !!value.publishOutputs,
    };
    this.saving.set(true);
    try {
      const loaded = this.loadedProjectSchedule;
      if (!loaded) {
        await firstValueFrom(this.projectApi.createSchedule(projectId, request));
        this.toast.success('Schedule created', `"${request.label}" will run on the cadence you set.`);
      } else {
        const changes = changedFields(loaded, request);
        if (Object.keys(changes).length > 0) {
          await firstValueFrom(this.projectApi.updateSchedule(projectId, loaded.scheduleId, changes));
        }
        this.toast.success('Schedule updated');
      }
      void this.router.navigate(this.backLink());
    } catch (err) {
      this.toast.error('Could not save schedule', projectErrorMessage(err, 'Failed to save the schedule'));
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
    if (this.offersWatchFields()) {
      // The model is sent only when it changed, so an edit to something else never
      // re-checks (and refuses) a model the user has since lost access to. Back to
      // "Default" is an explicit clear, since a bare null means "leave unchanged".
      request.mode = value.mode;
      const modelId = value.modelId || null;
      if (modelId !== this.loadedModelId) {
        if (modelId) request.modelId = modelId;
        else request.clearModel = true;
      }
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
    if (this.runInFlight()) {
      return;
    }
    const promptText = this.form.controls.promptText.value?.trim();
    if (!promptText) {
      this.form.controls.promptText.setErrors({ required: true });
      this.form.controls.promptText.markAsTouched();
      return;
    }

    // In a project, the run is a task of the caller's own on the project's assistant.
    const agentId = this.isProject()
      ? (this.project()?.harnessAgentId ?? null)
      : (this.form.controls.assistantId.value ?? null);
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
    this.router.navigate(this.backLink());
  }
}

/**
 * The fields of a project schedule's edit that differ from what was loaded. The
 * server records every field it is sent as changed, so sending only these keeps
 * the project's Activity line true ("changed the prompt", not every field).
 */
export function changedFields(loaded: ProjectSchedule, next: CreateProjectScheduleRequest): UpdateProjectScheduleRequest {
  const changes: UpdateProjectScheduleRequest = {};
  const keys = Object.keys(next) as Array<keyof CreateProjectScheduleRequest>;
  for (const key of keys) {
    const was = loaded[key] ?? null;
    const now = next[key] ?? null;
    if (was !== now) {
      (changes as Record<string, unknown>)[key] = next[key];
    }
  }
  return changes;
}
