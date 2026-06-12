import { Component, ChangeDetectionStrategy, inject, input, output, signal, computed, effect, ElementRef } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark, heroCheck, heroChevronDown, heroChevronRight, heroArrowPath } from '@ng-icons/heroicons/outline';
import { ModelService } from '../../session/services/model/model.service';
import { ToolService, Tool } from '../../services/tool/tool.service';
import { SkillService } from '../../services/skill/skill.service';
import { ChatMode, ChatModeService } from '../../services/chat-mode/chat-mode.service';
import { SystemPromptsService } from '../../services/system-prompts/system-prompts.service';
import {
  KNOWN_PARAMS,
  KnownParamMeta,
  ManagedModel,
  ModelParamSpec,
  ModelProvider,
} from '../../admin/manage-models/models/managed-model.model';

/** Resolved row the template renders for a single inference param. */
interface AdvancedParamRow {
  key: string;
  meta: KnownParamMeta;
  spec: ModelParamSpec;
  /** Effective min/max after merging catalog defaults with the model's spec. */
  min: number | null;
  max: number | null;
  /** Current effective value: user override if set, else the admin default. */
  value: unknown;
  /** True when the user has overridden the admin default. */
  isOverridden: boolean;
  /** Disabled because another active param's `incompatibleWith` includes us. */
  disabledByConflict: boolean;
  /** Locked by the admin — show the value but block edits. */
  locked: boolean;
  /**
   * Set when the row's effective bounds collapse to nothing — e.g. thinking's
   * floor (1024) is above the current `max_tokens − 1` cap. Surfacing this as
   * a separate flag (vs. just disabling) lets the template explain *why*.
   */
  unsatisfiable?: { reason: string };
}

@Component({
  selector: 'app-model-settings',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroXMark, heroCheck, heroChevronDown, heroChevronRight, heroArrowPath })],
  host: {
    '(document:click)': 'onDocumentClick($event)',
  },
  templateUrl: './model-settings.html',
  styleUrl: './model-settings.css',
})
export class ModelSettings {
  private elementRef = inject(ElementRef);
  protected modelService = inject(ModelService);
  protected toolService = inject(ToolService);
  protected skillService = inject(SkillService);
  protected chatModeService = inject(ChatModeService);
  protected systemPromptsService = inject(SystemPromptsService);

  // Input to control visibility
  isOpen = input<boolean>(false);

  // Session ID needed to persist prompt selection
  sessionId = input<string | null>(null);

  // Track if panel has ever been opened to avoid initial animation
  protected hasBeenOpened = signal(false);

  // Model dropdown state
  protected isModelDropdownOpen = signal(false);
  protected focusedOptionIndex = signal(-1);

  // Advanced section collapse state. Default closed so the panel doesn't
  // grow taller for users who never touch inference params.
  protected isAdvancedOpen = signal(false);
  protected isToolsOpen = signal(false);
  protected isSkillsOpen = signal(false);

  // Per-param transient "clamped to N" notice keyed by param key. Cleared
  // ~3s after it's set or the moment the user edits the row again.
  protected clampNotices = signal<Record<string, string>>({});
  private clampTimers = new Map<string, ReturnType<typeof setTimeout>>();
  private static readonly CLAMP_NOTICE_MS = 3000;

  // Output event when panel should close
  closed = output<void>();

  /** Effective merged inference-param view for the current model. */
  protected readonly advancedRows = computed<AdvancedParamRow[]>(() => {
    const model = this.modelService.selectedModel();
    const spec = model?.supportedParams?.params ?? {};
    const overrides = this.modelService.selectedModelOverrides();

    // Active = user override if set, else admin default. Drives the
    // incompatibility gate (e.g. thinking suppresses sampling params).
    const activeValues: Record<string, unknown> = {};
    for (const [key, paramSpec] of Object.entries(spec)) {
      if (!paramSpec.supported) continue;
      const override = overrides[key];
      activeValues[key] = override !== undefined ? override : paramSpec.default;
    }

    const conflictedKeys = new Set<string>();
    for (const meta of KNOWN_PARAMS) {
      if (!meta.incompatibleWith?.length) continue;
      const value = activeValues[meta.key];
      if (!value) continue;
      for (const conflict of meta.incompatibleWith) conflictedKeys.add(conflict);
    }

    // Anthropic requires `thinking budget < max_tokens`. Compute the
    // effective max_tokens (override > admin default > provider bounds)
    // so the thinking row's max input never exceeds budget − 1.
    const maxTokensSpec = spec['max_tokens'];
    const maxTokensActive = activeValues['max_tokens'];
    const maxTokensProviderBounds =
      KNOWN_PARAMS.find((p) => p.key === 'max_tokens')?.defaults?.[
        (model?.provider ?? 'bedrock') as ModelProvider
      ];
    const effectiveMaxTokens =
      typeof maxTokensActive === 'number'
        ? maxTokensActive
        : typeof maxTokensSpec?.default === 'number'
          ? maxTokensSpec.default
          : (maxTokensSpec?.max ?? maxTokensProviderBounds?.max ?? null);

    const rows: AdvancedParamRow[] = [];
    for (const meta of KNOWN_PARAMS) {
      const paramSpec = spec[meta.key];
      if (!paramSpec || !paramSpec.supported) continue;
      const provider = (model?.provider ?? 'bedrock') as ModelProvider;
      if (!meta.providers.includes(provider)) continue;
      const providerBounds = meta.defaults?.[provider];
      const min =
        paramSpec.min ??
        providerBounds?.min ??
        meta.defaultMin ??
        null;
      let max =
        paramSpec.max ??
        providerBounds?.max ??
        meta.defaultMax ??
        null;
      let unsatisfiable: { reason: string } | undefined;
      // Tighten the thinking budget cap to max_tokens − 1 so the form can't
      // produce a request the Bedrock validator will reject. If the resulting
      // window collapses (cap < min), mark the row unsatisfiable so the
      // template can disable the toggle and explain *why* — otherwise the
      // user can set it to a value that's both inside the input's HTML
      // bounds and rejected at request time.
      if (meta.key === 'thinking' && effectiveMaxTokens !== null) {
        const cap = effectiveMaxTokens - 1;
        max = max === null ? cap : Math.min(max, cap);
        if (min !== null && max !== null && max < min) {
          unsatisfiable = {
            reason:
              `Set Max Output Tokens above ${min} to enable extended thinking ` +
              `(currently ${effectiveMaxTokens}).`,
          };
        }
      }
      const override = overrides[meta.key];
      const value = override !== undefined ? override : paramSpec.default ?? null;
      rows.push({
        key: meta.key,
        meta,
        spec: paramSpec,
        min,
        max,
        value,
        isOverridden: override !== undefined,
        disabledByConflict: conflictedKeys.has(meta.key),
        locked: !!paramSpec.locked,
        unsatisfiable,
      });
    }
    return rows;
  });

  protected readonly hasAdvancedParams = computed(() => this.advancedRows().length > 0);

  protected readonly overriddenCount = computed(
    () => this.advancedRows().filter((row) => row.isOverridden).length,
  );

  constructor() {
    // Track when panel is first opened and manage body scroll
    effect(() => {
      const isOpen = this.isOpen();

      if (isOpen && !this.hasBeenOpened()) {
        this.hasBeenOpened.set(true);
      }

      // Prevent background scrolling when panel is open
      if (isOpen) {
        document.body.style.overflow = 'hidden';
      } else {
        document.body.style.overflow = '';
      }
    });
  }

  onDocumentClick(event: MouseEvent): void {
    // Close dropdown if clicking outside
    if (this.isModelDropdownOpen() && !this.elementRef.nativeElement.contains(event.target)) {
      this.isModelDropdownOpen.set(false);
    }
  }

  close(): void {
    this.closed.emit();
  }

  toggleModelDropdown(): void {
    this.isModelDropdownOpen.update(open => !open);
    if (this.isModelDropdownOpen()) {
      // Set focus to currently selected model
      const models = this.modelService.availableModels();
      const selectedModel = this.modelService.selectedModel();
      const selectedIndex = models.findIndex(m => m.modelId === selectedModel?.modelId);
      this.focusedOptionIndex.set(selectedIndex >= 0 ? selectedIndex : 0);
    }
  }

  selectModel(model: ManagedModel): void {
    this.modelService.setSelectedModel(model);
    this.isModelDropdownOpen.set(false);
  }

  isModelSelected(model: ManagedModel): boolean {
    return this.modelService.selectedModel()?.modelId === model.modelId;
  }

  onDropdownKeydown(event: KeyboardEvent): void {
    const models = this.modelService.availableModels();
    const currentIndex = this.focusedOptionIndex();

    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        if (!this.isModelDropdownOpen()) {
          this.isModelDropdownOpen.set(true);
          this.focusedOptionIndex.set(0);
        } else {
          this.focusedOptionIndex.set(Math.min(currentIndex + 1, models.length - 1));
        }
        break;
      case 'ArrowUp':
        event.preventDefault();
        if (this.isModelDropdownOpen()) {
          this.focusedOptionIndex.set(Math.max(currentIndex - 1, 0));
        }
        break;
      case 'Enter':
      case ' ':
        event.preventDefault();
        if (this.isModelDropdownOpen() && currentIndex >= 0 && currentIndex < models.length) {
          this.selectModel(models[currentIndex]);
        } else {
          this.toggleModelDropdown();
        }
        break;
      case 'Escape':
        event.preventDefault();
        this.isModelDropdownOpen.set(false);
        break;
      case 'Tab':
        this.isModelDropdownOpen.set(false);
        break;
    }
  }

  /** Which MCP server rows are expanded to show their per-tool toggles. */
  protected expandedServers = signal<Set<string>>(new Set());
  /** Servers with a live discovery request in flight. */
  protected discoveringServers = signal<Set<string>>(new Set());
  /** Per-server discovery error messages. */
  protected discoverError = signal<Record<string, string>>({});

  toggleTool(toolId: string): void {
    this.toolService.toggleTool(toolId);
  }

  /** True when a tool is an MCP server that supports per-tool enablement. */
  isMcpServer(tool: Tool): boolean {
    return tool.protocol === 'mcp' || tool.protocol === 'mcp_external';
  }

  isServerExpanded(toolId: string): boolean {
    return this.expandedServers().has(toolId);
  }

  /** "3 of 8 tools enabled" when a server is partially enabled, else null. */
  partialServerLabel(tool: Tool): string | null {
    const subs = tool.serverTools ?? [];
    if (subs.length === 0) return null;
    const on = subs.filter((s) => s.enabled).length;
    if (on === 0 || on === subs.length) return null;
    return `${on} of ${subs.length} tools enabled`;
  }

  toggleServerExpanded(toolId: string): void {
    this.expandedServers.update((set) => {
      const next = new Set(set);
      if (next.has(toolId)) {
        next.delete(toolId);
      } else {
        next.add(toolId);
      }
      return next;
    });
  }

  toggleServerTool(toolId: string, name: string): void {
    this.toolService.toggleServerTool(toolId, name);
  }

  async discoverServerTools(tool: Tool): Promise<void> {
    this.discoveringServers.update((s) => new Set(s).add(tool.toolId));
    this.discoverError.update((m) => {
      const next = { ...m };
      delete next[tool.toolId];
      return next;
    });
    try {
      await this.toolService.discoverServerTools(tool.toolId);
    } catch {
      this.discoverError.update((m) => ({
        ...m,
        [tool.toolId]: 'Could not list this server’s tools.',
      }));
    } finally {
      this.discoveringServers.update((s) => {
        const next = new Set(s);
        next.delete(tool.toolId);
        return next;
      });
    }
  }

  selectPrompt(promptId: string | null): void {
    const sid = this.sessionId();
    this.systemPromptsService.setActivePrompt(sid, promptId)
      .catch(err => console.error('Failed to persist prompt selection:', err));
  }

  toggleAdvanced(): void {
    this.isAdvancedOpen.update((open) => !open);
  }

  toggleTools(): void {
    this.isToolsOpen.update((open) => !open);
  }

  toggleSkills(): void {
    this.isSkillsOpen.update((open) => !open);
  }

  setMode(mode: ChatMode): void {
    this.chatModeService.setMode(mode, this.sessionId());
  }

  toggleSkill(skillId: string): void {
    this.skillService.toggleSkill(skillId)
      .catch(err => console.error('Failed to toggle skill:', err));
  }

  /**
   * Read a coerced value off a number/range input. Returns `null` when the
   * field is empty so the override is cleared rather than stored as 0.
   */
  protected readNumberInput(event: Event): number | null {
    const target = event.target as HTMLInputElement | null;
    if (!target || target.value === '') return null;
    const parsed = Number(target.value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  onParamNumberChange(row: AdvancedParamRow, event: Event): void {
    if (row.locked || row.disabledByConflict) return;
    const raw = this.readNumberInput(event);
    if (raw === null) {
      this.clearClampNotice(row.key);
      this.modelService.setInferenceParamOverride(row.key, null);
      this.maybeAdjustThinkingForMaxTokens(row.key);
      return;
    }
    const clamped = this.applyBounds(raw, row.min, row.max);
    if (clamped !== raw) {
      this.flashClampNotice(row.key, this.formatClampMessage(row, clamped));
      // Reflect the clamped value back into the input so the visible value
      // matches what we'll actually send. Browser number inputs already
      // refuse out-of-range submissions, but typed values can survive blur.
      const target = event.target as HTMLInputElement | null;
      if (target) target.value = String(clamped);
    } else {
      this.clearClampNotice(row.key);
    }
    this.modelService.setInferenceParamOverride(row.key, clamped);
    this.maybeAdjustThinkingForMaxTokens(row.key);
  }

  onParamToggle(row: AdvancedParamRow): void {
    if (row.locked || row.disabledByConflict) return;
    const next = !row.value;
    this.modelService.setInferenceParamOverride(row.key, next);
  }

  /**
   * Enum-select params (e.g. `effort`). The empty option clears the override
   * (fall back to the admin default), mirroring how emptying a number input
   * clears it. Any non-empty value is sent verbatim; the server gates it
   * against the model's `allowed` set, so an out-of-domain value can't slip
   * through even if the option list is momentarily stale.
   */
  onParamSelectChange(row: AdvancedParamRow, event: Event): void {
    if (row.locked || row.disabledByConflict) return;
    const target = event.target as HTMLSelectElement | null;
    const raw = target?.value ?? '';
    this.modelService.setInferenceParamOverride(row.key, raw === '' ? null : raw);
  }

  /**
   * Extended thinking enable/disable. The stored value is `null` (off) or an
   * int budget (on). Default budget falls back to the admin default, then to
   * the catalog `defaultMin` (1024 for thinking).
   *
   * Refuses to enable when ``row.unsatisfiable`` is set — i.e. when the
   * effective max_tokens window can't accommodate the budget floor. The
   * template also disables the toggle in that state; this guard is defense
   * in depth for keyboard/programmatic toggles.
   */
  onThinkingToggle(row: AdvancedParamRow): void {
    if (row.locked || row.disabledByConflict) return;
    if (row.value) {
      this.modelService.setInferenceParamOverride(row.key, null);
      return;
    }
    if (row.unsatisfiable) return;
    const fallback =
      (typeof row.spec.default === 'number' ? row.spec.default : null) ??
      row.meta.defaultMin ??
      1024;
    // Pin the seed budget to the row's effective range so we never store a
    // value the input itself would reject.
    const seeded = this.applyBounds(fallback, row.min, row.max);
    this.modelService.setInferenceParamOverride(row.key, seeded);
  }

  onThinkingBudgetChange(row: AdvancedParamRow, event: Event): void {
    if (row.locked || row.disabledByConflict) return;
    const raw = this.readNumberInput(event);
    if (raw === null || raw <= 0) {
      this.clearClampNotice(row.key);
      this.modelService.setInferenceParamOverride(row.key, null);
      return;
    }
    // Clamp to the row's effective bounds (which already incorporate the
    // `max_tokens − 1` cap from the advancedRows computation). Defensive
    // because number inputs accept out-of-range values via keyboard.
    const floored = Math.floor(raw);
    const clamped = this.applyBounds(floored, row.min, row.max);
    if (clamped !== floored) {
      this.flashClampNotice(row.key, this.formatClampMessage(row, clamped));
      const target = event.target as HTMLInputElement | null;
      if (target) target.value = String(clamped);
    } else {
      this.clearClampNotice(row.key);
    }
    this.modelService.setInferenceParamOverride(row.key, clamped);
  }

  resetParam(row: AdvancedParamRow): void {
    if (row.locked) return;
    this.modelService.setInferenceParamOverride(row.key, null);
  }

  resetAllParams(): void {
    this.modelService.clearInferenceParamOverrides();
  }

  /** Template helper: extended thinking is on when value is a positive number. */
  protected isThinkingEnabled(value: unknown): boolean {
    return typeof value === 'number' && value > 0;
  }

  private applyBounds(value: number, min: number | null, max: number | null): number {
    if (min !== null && value < min) return min;
    if (max !== null && value > max) return max;
    return value;
  }

  /**
   * After a max_tokens edit, re-check the thinking row's invariant
   * (budget < max_tokens) and clear the budget if the new ceiling can no
   * longer accommodate it. Avoids the "user lowers max_tokens, thinking
   * silently violates invariant, request 400s at Bedrock" failure mode.
   *
   * No-op for any other param edit. Kept lazy and side-effecting so the
   * `advancedRows` computed stays read-only — mutating model overrides
   * inside a computed would create a glitchy reactive cycle.
   */
  private maybeAdjustThinkingForMaxTokens(changedKey: string): void {
    if (changedKey !== 'max_tokens') return;
    const thinking = this.advancedRows().find((r) => r.key === 'thinking');
    if (!thinking) return;
    if (!this.isThinkingEnabled(thinking.value)) return;
    // Row was just rebuilt off the latest max_tokens — `unsatisfiable` is set
    // when the floor (1024) now exceeds max_tokens-1, and `max` is the new
    // cap otherwise. Either way, drop the override and tell the user.
    if (thinking.unsatisfiable) {
      this.modelService.setInferenceParamOverride(thinking.key, null);
      this.flashClampNotice(
        thinking.key,
        'Extended thinking turned off — Max Output Tokens is below the 1024 budget floor.',
      );
      return;
    }
    if (typeof thinking.value === 'number' && thinking.max !== null && thinking.value > thinking.max) {
      const reduced = thinking.max;
      this.modelService.setInferenceParamOverride(thinking.key, reduced);
      this.flashClampNotice(
        thinking.key,
        `Reduced to ${reduced} to stay below Max Output Tokens.`,
      );
    }
  }

  /**
   * Phrase the clamp notice based on which bound was hit and which row it was
   * — the thinking row mentions the max_tokens coupling explicitly because
   * that's the most likely surprise for the user.
   */
  private formatClampMessage(row: AdvancedParamRow, clampedTo: number): string {
    if (row.key === 'thinking' && row.max !== null && clampedTo === row.max) {
      return `Reduced to ${clampedTo} to stay below Max Output Tokens.`;
    }
    if (row.min !== null && clampedTo === row.min) {
      return `Raised to ${clampedTo} (minimum allowed by this model).`;
    }
    return `Reduced to ${clampedTo} (maximum allowed by this model).`;
  }

  private flashClampNotice(key: string, message: string): void {
    this.clampNotices.update((current) => ({ ...current, [key]: message }));
    const existing = this.clampTimers.get(key);
    if (existing) clearTimeout(existing);
    const handle = setTimeout(() => {
      this.clearClampNotice(key);
    }, ModelSettings.CLAMP_NOTICE_MS);
    this.clampTimers.set(key, handle);
  }

  private clearClampNotice(key: string): void {
    const existing = this.clampTimers.get(key);
    if (existing) {
      clearTimeout(existing);
      this.clampTimers.delete(key);
    }
    this.clampNotices.update((current) => {
      if (!(key in current)) return current;
      const next = { ...current };
      delete next[key];
      return next;
    });
  }
}
