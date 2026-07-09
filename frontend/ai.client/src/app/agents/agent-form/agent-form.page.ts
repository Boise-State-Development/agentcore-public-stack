import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
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
} from '@ng-icons/heroicons/outline';
import { Dialog } from '@angular/cdk/dialog';
import { PickerComponent } from '@ctrl/ngx-emoji-mart';
import { CdkConnectedOverlay, CdkOverlayOrigin, ConnectedPosition } from '@angular/cdk/overlay';
import { AgentService } from '../services/agent.service';
import { AgentBinding, BindableItem, MemorySpaceBindingConfig } from '../models/agent.model';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { ThemeService } from '../../components/topnav/components/theme-toggle/theme.service';
import { ToastService } from '../../services/toast/toast.service';
import { TooltipDirective } from '../../components/tooltip/tooltip.directive';
import {
  ShareAssistantDialogComponent,
  ShareAssistantDialogData,
} from '../../assistants/components/share-assistant-dialog.component';
import { KnowledgeBaseSectionComponent } from '../../knowledge-base/knowledge-base-section.component';

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
  readonly selectedToolRefs = signal<Set<string>>(new Set());
  readonly selectedSkillRefs = signal<Set<string>>(new Set());
  readonly memorySelections = signal<MemorySelection[]>([]);

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
    this.selectedModelId.set(this.selectedModelId() === ref ? null : ref);
  }

  // ---- tools / skills (multi-select toggles) ---------------------------
  toggleTool(ref: string): void {
    this.selectedToolRefs.update((set) => toggle(set, ref));
  }
  isToolSelected(ref: string): boolean {
    return this.selectedToolRefs().has(ref);
  }
  toggleSkill(ref: string): void {
    this.selectedSkillRefs.update((set) => toggle(set, ref));
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
  }
  /** readwrite requires editor+ on the space (D5) — the option is disabled otherwise. */
  canWrite(sel: MemorySelection): boolean {
    return sel.role === 'owner' || sel.role === 'editor';
  }
  setAccess(ref: string, access: 'read' | 'readwrite'): void {
    this.memorySelections.update((cur) =>
      cur.map((m) => (m.ref === ref ? { ...m, access } : m)),
    );
  }
  toggleAlwaysLoad(ref: string): void {
    this.memorySelections.update((cur) =>
      cur.map((m) => (m.ref === ref ? { ...m, alwaysLoadIndex: !m.alwaysLoadIndex } : m)),
    );
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

  async onSubmit(): Promise<void> {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }
    if (!this.selectedModelId()) {
      this.toast.error('Select a model for this agent.');
      return;
    }

    const v = this.form.value;
    const payload = {
      name: v.name,
      description: v.description,
      instructions: v.instructions,
      visibility: v.visibility,
      tags: v.tags ?? [],
      starters: this.starters.value ?? [],
      emoji: v.emoji || undefined,
      modelConfig: { modelId: this.selectedModelId()! },
      bindings: this.buildBindings(),
    };

    this.saving.set(true);
    try {
      if (this.mode() === 'create') {
        await this.agentService.createAgent(payload);
      } else {
        await this.agentService.updateAgent(this.agentId()!, { ...payload, status: 'COMPLETE' });
      }
      this.toast.success('Agent saved.');
      this.router.navigate(['/agents']);
    } catch (err: unknown) {
      const detail = (err as { error?: { detail?: string } } | null)?.error?.detail;
      this.toast.error(detail ?? 'Could not save the agent.');
    } finally {
      this.saving.set(false);
    }
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
