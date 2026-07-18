import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
  ElementRef,
  OnInit,
  OnDestroy,
} from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import {
  ReactiveFormsModule,
  FormBuilder,
  FormGroup,
  FormArray,
  FormControl,
  Validators,
} from '@angular/forms';
import { Subscription, firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroFaceSmile,
  heroXMark,
  heroPlus,
  heroTrash,
  heroShare,
  heroCpuChip,
  heroWrenchScrewdriver,
  heroSparkles,
  heroCircleStack,
  heroCheck,
  heroAdjustmentsHorizontal,
} from '@ng-icons/heroicons/outline';
import { Dialog } from '@angular/cdk/dialog';
import { PickerComponent } from '@ctrl/ngx-emoji-mart';
import { CdkConnectedOverlay, CdkOverlayOrigin, ConnectedPosition } from '@angular/cdk/overlay';
import { AgentService } from '../services/agent.service';
import {
  AgentBinding,
  BindableItem,
  MemorySpaceBindingConfig,
  ModelParamSpec,
  SupportedParams,
} from '../models/agent.model';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { ThemeService } from '../../components/topnav/components/theme-toggle/theme.service';
import { ToastService } from '../../services/toast/toast.service';
import { TooltipDirective } from '../../components/tooltip/tooltip.directive';
import { AgentPreviewComponent } from './components/agent-preview.component';
import {
  ShareAssistantDialogComponent,
  ShareAssistantDialogData,
} from '../../assistants/components/share-assistant-dialog.component';
import { KnowledgeBaseSectionComponent } from '../../knowledge-base/knowledge-base-section.component';

/** A model param rendered as an editable control (numeric or enum). */
interface ParamView {
  key: string;
  label: string;
  spec: ModelParamSpec;
  step: number; // for numeric inputs
}

/** Friendly labels for the canonical param keys the Designer commonly exposes. */
const PARAM_LABELS: Record<string, string> = {
  temperature: 'Temperature',
  max_tokens: 'Max tokens',
  top_p: 'Top P',
  top_k: 'Top K',
  reasoning_effort: 'Reasoning effort',
  effort: 'Reasoning effort',
};

/** A memory-space selection with its per-binding config (access + alwaysLoad). */
interface MemorySelection {
  ref: string;
  label: string;
  role: string;
  access: 'read' | 'readwrite';
  alwaysLoadIndex: boolean; // maps to alwaysLoad: ['MEMORY.md']
}

/**
 * Agent Designer — the authoring surface (Phase 4). A persona + a governed model
 * single-select + binding pickers (tools, skills, memory spaces), each populated
 * from `GET /agents/bindable?kind=…` so the user only sees what their role enables
 * (D4). The KB is welded to the agent (synthesized on read, not author-settable) so
 * it is shown read-only. Mirrors the write-side rules in `binding_validation`.
 */
@Component({
  selector: 'app-agent-form-page',
  templateUrl: './agent-form.page.html',
  styleUrl: './agent-form.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ReactiveFormsModule,
    NgIcon,
    RouterLink,
    PickerComponent,
    CdkOverlayOrigin,
    CdkConnectedOverlay,
    TooltipDirective,
    AgentPreviewComponent,
    KnowledgeBaseSectionComponent,
  ],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroFaceSmile,
      heroXMark,
      heroPlus,
      heroTrash,
      heroShare,
      heroCpuChip,
      heroWrenchScrewdriver,
      heroSparkles,
      heroCircleStack,
      heroCheck,
      heroAdjustmentsHorizontal,
    }),
  ],
})
export class AgentFormPage implements OnInit, OnDestroy {
  private fb = inject(FormBuilder);
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  private agentService = inject(AgentService);
  private sidenavService = inject(SidenavService);
  private themeService = inject(ThemeService);
  private toast = inject(ToastService);
  private dialog = inject(Dialog);
  private host = inject(ElementRef<HTMLElement>);

  form!: FormGroup;
  private formSub?: Subscription;

  readonly agentId = signal<string | null>(null);
  readonly saving = signal(false);
  readonly loadingAgent = signal(false);
  readonly userPermission = signal<'owner' | 'editor' | 'viewer'>('owner');
  /**
   * Whether {@link userPermission} reflects a value loaded from the server.
   * The knowledge-base section waits on this before issuing its edit-gated
   * sync-policy calls (a viewer would 403 on the default 'owner' guess).
   */
  readonly permissionResolved = signal(false);
  readonly isEmojiPickerOpen = signal(false);
  readonly isDarkMode = this.themeService.theme;

  readonly mode = computed<'create' | 'edit'>(() => (this.agentId() ? 'edit' : 'create'));
  readonly isViewer = computed(() => this.userPermission() === 'viewer');

  // Bindable palettes (RBAC-filtered) + current selections.
  readonly models = signal<BindableItem[]>([]);
  readonly tools = signal<BindableItem[]>([]);
  readonly skills = signal<BindableItem[]>([]);
  readonly spaces = signal<BindableItem[]>([]);

  readonly selectedModelId = signal<string | null>(null);
  /** Author-set inference params (temperature/maxTokens/effort/…), governed by the
   * selected model's `supportedParams`. Empty ⇒ omit `params` (today's default). */
  readonly modelParams = signal<Record<string, number | string>>({});
  readonly selectedToolRefs = signal<Set<string>>(new Set());
  readonly selectedSkillRefs = signal<Set<string>>(new Set());
  readonly memorySelections = signal<MemorySelection[]>([]);

  // ---- model params (governed by the selected model's supportedParams) --------
  private readonly selectedModel = computed<BindableItem | undefined>(() =>
    this.models().find((m) => m.ref === this.selectedModelId()),
  );
  private readonly paramSpecs = computed<[string, ModelParamSpec][]>(() => {
    const supported = this.selectedModel()?.meta?.['supportedParams'] as SupportedParams | undefined;
    const params = supported?.params ?? {};
    return Object.entries(params).filter(([, spec]) => spec.supported);
  });
  /** Editable enum params (a fixed `allowed` domain) → rendered as a select. */
  readonly enumParams = computed<ParamView[]>(() =>
    this.paramSpecs()
      .filter(([, spec]) => !spec.locked && spec.allowed != null)
      .map(([key, spec]) => ({ key, label: paramLabel(key), spec, step: 1 })),
  );
  /** Editable numeric params → rendered as a bounded number input. */
  readonly numberParams = computed<ParamView[]>(() =>
    this.paramSpecs()
      .filter(([, spec]) => !spec.locked && spec.allowed == null)
      .map(([key, spec]) => ({ key, label: paramLabel(key), spec, step: paramStep(key) })),
  );
  /** Locked params — shown read-only so the author sees the admin-pinned value. */
  readonly lockedParams = computed<ParamView[]>(() =>
    this.paramSpecs()
      .filter(([, spec]) => spec.locked)
      .map(([key, spec]) => ({ key, label: paramLabel(key), spec, step: 1 })),
  );
  readonly hasParamControls = computed(
    () => this.enumParams().length > 0 || this.numberParams().length > 0 || this.lockedParams().length > 0,
  );

  // ---- live preview (side-by-side) --------------------------------------
  // Persona fields mirrored from the reactive form so the OnPush preview updates
  // as the user types (kept in sync via form.valueChanges → syncFormToSignals).
  readonly liveFormName = signal('');
  readonly liveFormDescription = signal('');
  readonly liveFormEmoji = signal('');
  readonly liveFormStarters = signal<string[]>([]);

  /** Model/params/bindings resolve from the SAVED record, so the preview needs a
   * save to reflect changes to them. Persona/instructions preview live. `form.dirty`
   * covers the persona fields; this flag covers the out-of-form binding signals. */
  private readonly bindingsDirty = signal(false);
  readonly isDirty = computed(() => this.form?.dirty === true || this.bindingsDirty());

  readonly previewModelLabel = computed<string | null>(() => this.selectedModel()?.label ?? null);

  readonly emojiPickerPositions: ConnectedPosition[] = [
    { originX: 'start', originY: 'bottom', overlayX: 'start', overlayY: 'top', offsetY: 8 },
    { originX: 'start', originY: 'top', overlayX: 'start', overlayY: 'bottom', offsetY: -8 },
  ];

  get starters(): FormArray {
    return this.form.get('starters') as FormArray;
  }

  ngOnInit(): void {
    this.sidenavService.hide();

    this.form = this.fb.group({
      name: ['', [Validators.required, Validators.minLength(3)]],
      description: ['', [Validators.required, Validators.minLength(10)]],
      instructions: ['', [Validators.required, Validators.minLength(20)]],
      visibility: ['PRIVATE'],
      tags: [[] as string[]],
      starters: this.fb.array([]),
      emoji: [''],
    });

    // Mirror form values into the live signals so the OnPush preview updates as the
    // user types; seed once for the initial (empty or, after loadAgent, patched) state.
    this.syncFormToSignals();
    this.formSub = this.form.valueChanges.subscribe(() => this.syncFormToSignals());

    // Load the RBAC-filtered palettes in parallel; then hydrate an existing agent.
    void this.loadPalettes();

    const id = this.route.snapshot.paramMap.get('id');
    this.agentId.set(id);
    if (id) {
      this.loadingAgent.set(true);
      void this.loadAgent(id).finally(() => {
        this.loadingAgent.set(false);
        // Permission is now resolved — release the knowledge-base section's
        // gate on its edit-gated sync-policy calls.
        this.permissionResolved.set(true);
      });
    } else {
      // Create mode: the user is implicitly the owner — no record to resolve.
      this.permissionResolved.set(true);
    }
  }

  ngOnDestroy(): void {
    this.sidenavService.show();
    this.formSub?.unsubscribe();
  }

  /** Push current form values into the live signals the preview reads. */
  private syncFormToSignals(): void {
    this.liveFormName.set(this.form.get('name')?.value || '');
    this.liveFormDescription.set(this.form.get('description')?.value || '');
    this.liveFormEmoji.set(this.form.get('emoji')?.value || '');
    this.liveFormStarters.set(this.starters.value || []);
  }

  private async loadPalettes(): Promise<void> {
    const [models, tools, skills, spaces] = await Promise.all([
      this.agentService.loadBindable('model'),
      this.agentService.loadBindable('tool'),
      this.agentService.loadBindable('skill'),
      this.agentService.loadBindable('memory_space'),
    ]);
    this.models.set(models);
    this.tools.set(tools);
    this.skills.set(skills);
    this.spaces.set(spaces);
  }

  private async loadAgent(id: string): Promise<void> {
    try {
      const agent = await this.agentService.getAgent(id);
      this.userPermission.set(agent.userPermission ?? 'owner');
      this.form.patchValue({
        name: agent.name,
        description: agent.description,
        instructions: agent.instructions,
        visibility: agent.visibility,
        tags: agent.tags ?? [],
        emoji: agent.emoji ?? '',
      });
      this.starters.clear();
      (agent.starters ?? []).forEach((s) => this.starters.push(new FormControl(s, Validators.required)));

      this.selectedModelId.set(agent.modelConfig?.modelId ?? null);
      this.modelParams.set(
        (agent.modelConfig?.params ?? {}) as Record<string, number | string>,
      );

      const toolRefs = new Set<string>();
      const skillRefs = new Set<string>();
      const memory: MemorySelection[] = [];
      for (const b of agent.bindings ?? []) {
        if (b.kind === 'tool') toolRefs.add(b.ref);
        else if (b.kind === 'skill') skillRefs.add(b.ref);
        else if (b.kind === 'memory_space') {
          const cfg = (b.config ?? {}) as Partial<MemorySpaceBindingConfig>;
          memory.push({
            ref: b.ref,
            label: this.spaceLabel(b.ref),
            role: this.spaceRole(b.ref),
            access: cfg.access === 'readwrite' ? 'readwrite' : 'read',
            alwaysLoadIndex: (cfg.alwaysLoad ?? []).includes('MEMORY.md'),
          });
        }
        // knowledge_base bindings are welded/synthesized and managed live by
        // the knowledge-base section — no read-only display state to hydrate.
      }
      this.selectedToolRefs.set(toolRefs);
      this.selectedSkillRefs.set(skillRefs);
      this.memorySelections.set(memory);
      // Freshly loaded state is clean — the preview matches the saved record.
      this.syncFormToSignals();
      this.form.markAsPristine();
      this.bindingsDirty.set(false);
    } catch (err) {
      console.error('Error loading agent:', err);
      this.toast.error('Could not load this agent.');
    }
  }

  private spaceLabel(ref: string): string {
    return this.spaces().find((s) => s.ref === ref)?.label ?? ref;
  }

  private spaceRole(ref: string): string {
    return (this.spaces().find((s) => s.ref === ref)?.meta?.['role'] as string) ?? 'viewer';
  }

  // ---- persona helpers -------------------------------------------------
  addStarter(): void {
    this.starters.push(new FormControl('', Validators.required));
  }
  removeStarter(index: number): void {
    this.starters.removeAt(index);
  }
  toggleEmojiPicker(): void {
    this.isEmojiPickerOpen.update((o) => !o);
  }
  closeEmojiPicker(): void {
    this.isEmojiPickerOpen.set(false);
  }
  onEmojiSelect(event: { emoji: { native: string } }): void {
    this.form.patchValue({ emoji: event.emoji.native });
    this.closeEmojiPicker();
  }
  clearEmoji(): void {
    this.form.patchValue({ emoji: '' });
  }

  // ---- tags ------------------------------------------------------------
  get tags(): string[] {
    return (this.form.get('tags')?.value as string[]) ?? [];
  }
  addTag(value: string): void {
    const t = value.trim();
    if (t && !this.tags.includes(t)) {
      this.form.get('tags')?.setValue([...this.tags, t]);
    }
  }
  removeTag(tag: string): void {
    this.form.get('tags')?.setValue(this.tags.filter((x) => x !== tag));
  }

  getFieldError(field: string): string | null {
    const c = this.form.get(field);
    if (!c || !c.touched || !c.errors) return null;
    if (c.errors['required']) return 'This field is required';
    if (c.errors['minlength']) return `Minimum length is ${c.errors['minlength'].requiredLength} characters`;
    return null;
  }

  // ---- model -----------------------------------------------------------
  selectModel(ref: string): void {
    const next = this.selectedModelId() === ref ? null : ref;
    // Params are model-specific — a value valid on one model may be unsupported or
    // out-of-bounds on another. Drop them when the model changes so the author re-sets
    // against the new model's controls (and we never persist a stale param).
    if (next !== this.selectedModelId()) this.modelParams.set({});
    this.selectedModelId.set(next);
    this.bindingsDirty.set(true);
  }

  // ---- model params ----------------------------------------------------
  /** Current value for a param, or the spec default (shown as a placeholder). */
  paramValue(key: string): number | string | '' {
    const v = this.modelParams()[key];
    return v === undefined ? '' : v;
  }
  onNumberParam(key: string, raw: string): void {
    if (raw === '') {
      this.clearParam(key);
      return;
    }
    const n = Number(raw);
    if (Number.isNaN(n)) return;
    this.modelParams.update((p) => ({ ...p, [key]: n }));
    this.bindingsDirty.set(true);
  }
  onEnumParam(key: string, value: string): void {
    if (value === '') {
      this.clearParam(key);
      return;
    }
    this.modelParams.update((p) => ({ ...p, [key]: value }));
    this.bindingsDirty.set(true);
  }
  private clearParam(key: string): void {
    this.modelParams.update((p) => {
      const next = { ...p };
      delete next[key];
      return next;
    });
    this.bindingsDirty.set(true);
  }

  // ---- tools / skills (multi-select toggles) ---------------------------
  toggleTool(ref: string): void {
    this.selectedToolRefs.update((set) => toggle(set, ref));
    this.bindingsDirty.set(true);
  }
  isToolSelected(ref: string): boolean {
    return this.selectedToolRefs().has(ref);
  }
  toggleSkill(ref: string): void {
    this.selectedSkillRefs.update((set) => toggle(set, ref));
    this.bindingsDirty.set(true);
  }
  isSkillSelected(ref: string): boolean {
    return this.selectedSkillRefs().has(ref);
  }

  // ---- memory spaces ---------------------------------------------------
  isSpaceSelected(ref: string): boolean {
    return this.memorySelections().some((m) => m.ref === ref);
  }
  toggleSpace(item: BindableItem): void {
    this.memorySelections.update((cur) => {
      if (cur.some((m) => m.ref === item.ref)) {
        return cur.filter((m) => m.ref !== item.ref);
      }
      const role = (item.meta?.['role'] as string) ?? 'viewer';
      return [
        ...cur,
        { ref: item.ref, label: item.label, role, access: 'read', alwaysLoadIndex: true },
      ];
    });
    this.bindingsDirty.set(true);
  }
  /** readwrite requires editor+ on the space (D5) — the option is disabled otherwise. */
  canWrite(sel: MemorySelection): boolean {
    return sel.role === 'owner' || sel.role === 'editor';
  }
  setAccess(ref: string, access: 'read' | 'readwrite'): void {
    this.memorySelections.update((cur) =>
      cur.map((m) => (m.ref === ref ? { ...m, access } : m)),
    );
    this.bindingsDirty.set(true);
  }
  toggleAlwaysLoad(ref: string): void {
    this.memorySelections.update((cur) =>
      cur.map((m) => (m.ref === ref ? { ...m, alwaysLoadIndex: !m.alwaysLoadIndex } : m)),
    );
    this.bindingsDirty.set(true);
  }

  // ---- submit ----------------------------------------------------------
  private buildBindings(): AgentBinding[] {
    const bindings: AgentBinding[] = [];
    for (const ref of this.selectedToolRefs()) bindings.push({ kind: 'tool', ref });
    for (const ref of this.selectedSkillRefs()) bindings.push({ kind: 'skill', ref });
    for (const m of this.memorySelections()) {
      const config: Record<string, unknown> = { access: m.access };
      if (m.alwaysLoadIndex) config['alwaysLoad'] = ['MEMORY.md'];
      bindings.push({ kind: 'memory_space', ref: m.ref, config });
    }
    // KB bindings are welded/synthesized — never sent (backend rejects an explicit one).
    return bindings;
  }

  /**
   * Scroll the first invalid control into view and focus it. `markAllAsTouched`
   * alone only reveals the inline error, which is usually below the fold once the
   * author has scrolled down to the Model/Skills sections — an invalid save then
   * reads as a click that did nothing.
   */
  private revealFirstInvalidControl(): void {
    // Match on the element type rather than [formControlName]: the starters array
    // binds `[formControlName]="$index"`, which renders no attribute to select on.
    const first = (this.host.nativeElement as HTMLElement).querySelector<HTMLElement>(
      'form input.ng-invalid, form textarea.ng-invalid, form select.ng-invalid',
    );
    if (!first) return;
    first.scrollIntoView({ behavior: 'smooth', block: 'center' });
    // preventScroll: the smooth scroll above owns the movement; focus would jump it.
    first.focus({ preventScroll: true });
  }

  /** Save the current form and return the agent id, or null if it wasn't saved
   * (invalid form, no model, or a server error). Shows the error toast; the caller
   * decides where to go on success. */
  private async persist(): Promise<string | null> {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      this.revealFirstInvalidControl();
      this.toast.error('Fix the highlighted fields before saving.');
      return null;
    }
    if (!this.selectedModelId()) {
      this.toast.error('Select a model for this agent.');
      return null;
    }

    const v = this.form.value;
    const params = this.modelParams();
    // Persist the model's provider alongside its id. The runtime resolver needs
    // provider to route (e.g. Mantle models like `openai.gpt-5.4` go through the
    // Responses API, not Bedrock ConverseStream); without it the binding resolves
    // to provider=None and a Mantle model fails with an invalid-model-identifier
    // error. Mirrors what the normal chat path sends with every request.
    const provider = this.selectedModel()?.meta?.['provider'] as string | undefined;
    const payload = {
      name: v.name,
      description: v.description,
      instructions: v.instructions,
      visibility: v.visibility,
      tags: v.tags ?? [],
      starters: this.starters.value ?? [],
      emoji: v.emoji || undefined,
      // Omit `params` when empty so the agent falls back to today's exact resolution.
      modelConfig: {
        modelId: this.selectedModelId()!,
        ...(provider ? { provider } : {}),
        ...(Object.keys(params).length ? { params } : {}),
      },
      bindings: this.buildBindings(),
    };

    this.saving.set(true);
    try {
      let id: string;
      if (this.mode() === 'create') {
        const created = await this.agentService.createAgent(payload);
        id = created.agentId;
        // Transition to edit mode in place so the side-by-side preview (which needs a
        // persisted id to resolve bindings) lights up without leaving the page.
        this.agentId.set(id);
      } else {
        const updated = await this.agentService.updateAgent(this.agentId()!, {
          ...payload,
          status: 'COMPLETE',
        });
        id = updated.agentId;
      }
      // Saved state now matches the preview — clear the dirty banner. The preview
      // re-resolves bindings from the saved record on its next message, so no reset
      // is needed; a create just set agentId, which resets the preview via its effect.
      this.form.markAsPristine();
      this.bindingsDirty.set(false);
      return id;
    } catch (err: unknown) {
      const detail = (err as { error?: { detail?: string } } | null)?.error?.detail;
      this.toast.error(detail ?? 'Could not save the agent.');
      return null;
    } finally {
      this.saving.set(false);
    }
  }

  async onSubmit(): Promise<void> {
    const id = await this.persist();
    if (!id) return;
    this.toast.success('Agent saved.');
    this.router.navigate(['/agents']);
  }

  /** Save from the preview and stay on the page so the author can keep iterating. */
  async onPreviewSave(): Promise<void> {
    const id = await this.persist();
    if (id) this.toast.success('Agent saved.');
  }

  /** Open a real full-page chat scoped to this agent (bigger surface than the inline
   * preview; agentId == assistantId, same harness). Saves first for owners/editors so
   * the chat reflects the current edits; viewers open the last saved version. */
  async testInChat(): Promise<void> {
    let id = this.agentId();
    if (!this.isViewer()) {
      id = await this.persist();
    }
    if (!id) return;
    this.router.navigate(['/'], { queryParams: { assistantId: id } });
  }

  onCancel(): void {
    this.router.navigate(['/agents']);
  }

  /**
   * Ensure an agent record exists so knowledge-base documents have a parent to
   * attach to. Passed to the knowledge-base section as its create-draft
   * callback: in create mode the first content-adding action mints a draft and
   * the form is patched with its server-assigned fields. The record id doubles
   * as the assistant id the document pipeline keys on (`agentId == assistantId`).
   * Returns the agent id; throws if draft creation fails.
   */
  readonly createDraftAgent = async (): Promise<string> => {
    const draft = await this.agentService.createDraft({
      name: this.form.get('name')?.value || 'Untitled Agent',
    });
    this.agentId.set(draft.agentId);
    this.form.patchValue({
      name: draft.name,
      description: draft.description || '',
      instructions: draft.instructions || '',
      visibility: draft.visibility,
      tags: draft.tags ?? [],
      emoji: draft.emoji ?? '',
    });
    return draft.agentId;
  };

  openShareDialog(): void {
    const id = this.agentId();
    if (!id) return;
    this.dialog.open(ShareAssistantDialogComponent, {
      data: {
        assistant: {
          assistantId: id,
          name: this.form.get('name')?.value || 'Agent',
          visibility: this.form.get('visibility')?.value,
          userPermission: this.userPermission(),
        },
      } as unknown as ShareAssistantDialogData,
      hasBackdrop: false,
    });
  }
}

function toggle(set: Set<string>, ref: string): Set<string> {
  const next = new Set(set);
  if (next.has(ref)) next.delete(ref);
  else next.add(ref);
  return next;
}

function paramLabel(key: string): string {
  return PARAM_LABELS[key] ?? key.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
}

/** Token counts step by 1; everything else (temperature, top_p, …) by 0.1. */
function paramStep(key: string): number {
  return key.includes('token') || key === 'top_k' ? 1 : 0.1;
}
