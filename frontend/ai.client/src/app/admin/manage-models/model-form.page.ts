import { Component, ChangeDetectionStrategy, inject, signal, computed, OnInit } from '@angular/core';
import { Router, ActivatedRoute, RouterLink } from '@angular/router';
import {
  AbstractControl,
  FormBuilder,
  FormGroup,
  FormArray,
  FormControl,
  ValidationErrors,
  Validators,
  ReactiveFormsModule,
} from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowLeft, heroChevronDown, heroChevronRight } from '@ng-icons/heroicons/outline';
import {
  AVAILABLE_PROVIDERS,
  KNOWN_PARAMS,
  KnownParamMeta,
  MANTLE_ENDPOINT_PATHS,
  ManagedModelFormData,
  MantleEndpointPath,
  ModelParamSpec,
  ModelProvider,
  SupportedParams,
} from './models/managed-model.model';
import { ManagedModelsService } from './services/managed-models.service';
import { CuratedModelPrefillService } from './services/curated-model-prefill.service';
import { AppRolesService } from '../roles/services/app-roles.service';

interface ParamRowGroup {
  /**
   * Canonical param name. Read-only on known rows (seeded from the catalog,
   * no validators). Custom rows add `Validators.required +
   * Validators.pattern(CUSTOM_PARAM_KEY_PATTERN)`.
   *
   * Carrying `key` on every row lets the FormArray-level validator look up
   * `thinking` / `max_tokens` without depending on parallel signals.
   */
  key: FormControl<string>;
  supported: FormControl<boolean>;
  min: FormControl<number | null>;
  max: FormControl<number | null>;
  /**
   * Selectable subset for `kind: 'select'` params (e.g. `effort`). `null`
   * on numeric/toggle rows. The per-model effort tier difference lives
   * here as data, mirroring `ModelParamSpec.allowed` on the backend.
   */
  allowed: FormControl<(string | number)[] | null>;
  defaultValue: FormControl<number | boolean | string | null>;
  locked: FormControl<boolean>;
}

/**
 * Custom-param rows carry their canonical key as a form control so admins
 * can stage params the frontend catalog doesn't yet know about (e.g. a brand
 * new provider knob shipped before the next deploy).
 */
type CustomParamRowGroup = ParamRowGroup;

const CUSTOM_PARAM_KEY_PATTERN = /^[a-z][a-z0-9_]*$/;

/**
 * Cross-field validator for a single inference param row. Mirrors the
 * Pydantic `ModelParamSpec._check_bounds` rule on the backend so admins
 * see the failure inline instead of a 422 from the save action.
 *
 * Returns `null` when the row is unsupported (we don't care about bounds
 * on unsupported rows) or when nothing's wrong. Otherwise sets one of:
 *   - `minGreaterThanMax` — `min > max`
 *   - `defaultBelowMin`   — numeric default < min
 *   - `defaultAboveMax`   — numeric default > max
 */
function paramRowBoundsValidator(group: AbstractControl): ValidationErrors | null {
  const supported = group.get('supported')?.value;
  if (!supported) return null;
  const min = group.get('min')?.value;
  const max = group.get('max')?.value;
  const def = group.get('defaultValue')?.value;

  const errors: Record<string, true> = {};
  if (typeof min === 'number' && typeof max === 'number' && min > max) {
    errors['minGreaterThanMax'] = true;
  }
  if (typeof def === 'number') {
    if (typeof min === 'number' && def < min) errors['defaultBelowMin'] = true;
    if (typeof max === 'number' && def > max) errors['defaultAboveMax'] = true;
  }
  // Enum rows (`kind: 'select'`, e.g. effort) carry an `allowed` array
  // instead of min/max. The model must support at least one level, and the
  // default has to be one of them. Mirrors `ModelParamSpec._check_bounds`.
  const allowed = group.get('allowed')?.value;
  if (Array.isArray(allowed)) {
    if (allowed.length === 0) {
      errors['allowedEmpty'] = true;
    } else if (def !== null && def !== undefined && def !== '' && !allowed.includes(def)) {
      errors['defaultNotAllowed'] = true;
    }
  }
  return Object.keys(errors).length > 0 ? errors : null;
}

/**
 * FormArray-level validator that catches the two thinking-budget invariants
 * Anthropic enforces (and that `SupportedParams._check_thinking_invariants`
 * also enforces on the backend):
 *   - thinking budget must be >= 1024
 *   - thinking budget must be < the max_tokens default
 *
 * Errors are placed on the `thinking` row so the inline error markup picks
 * them up alongside per-row bounds errors.
 */
function thinkingInvariantsValidator(array: AbstractControl): ValidationErrors | null {
  if (!(array instanceof FormArray)) return null;
  let thinkingRow: FormGroup | undefined;
  let maxTokensRow: FormGroup | undefined;
  for (const row of array.controls as FormGroup[]) {
    const key = row.get('key')?.value;
    if (key === 'thinking') thinkingRow = row;
    else if (key === 'max_tokens') maxTokensRow = row;
  }
  if (!thinkingRow) return null;

  const supported = thinkingRow.get('supported')?.value;
  const def = thinkingRow.get('defaultValue')?.value;
  if (!supported || def === null || def === undefined || def === '' || def === 0) {
    // Clear any prior thinking-specific errors but keep bounds errors set
    // by the per-row validator (which runs independently).
    const existing = { ...(thinkingRow.errors ?? {}) };
    delete existing['thinkingBudgetTooLow'];
    delete existing['thinkingBudgetExceedsMaxTokens'];
    delete existing['thinkingBudgetNotNumeric'];
    thinkingRow.setErrors(Object.keys(existing).length > 0 ? existing : null);
    return null;
  }

  const errors: Record<string, true> = {};
  if (typeof def !== 'number' || Number.isNaN(def)) {
    errors['thinkingBudgetNotNumeric'] = true;
  } else {
    if (def < 1024) errors['thinkingBudgetTooLow'] = true;
    const maxTokensDef = maxTokensRow?.get('defaultValue')?.value;
    if (typeof maxTokensDef === 'number' && def >= maxTokensDef) {
      errors['thinkingBudgetExceedsMaxTokens'] = true;
    }
  }

  // Merge with any pre-existing per-row bounds errors so both are visible.
  const merged = { ...(thinkingRow.errors ?? {}), ...errors };
  thinkingRow.setErrors(Object.keys(merged).length > 0 ? merged : null);
  return null;
}

/**
 * FormArray-level validator pinning the `max_tokens` row to the model's
 * declared output ceiling. The model-level `maxOutputTokens` control is a
 * sibling of this FormArray (reached via `array.parent`) — neither the
 * `max` bound nor the `default` the runtime sends may exceed what the
 * model can physically produce.
 *
 * Errors land on the `max_tokens` row so the inline markup surfaces them
 * next to the per-row bounds errors. Mirrored on the backend by
 * `_max_tokens_within_ceiling` on `ManagedModelCreate`/`ManagedModelUpdate`.
 */
function maxTokensCeilingValidator(array: AbstractControl): ValidationErrors | null {
  if (!(array instanceof FormArray)) return null;
  let maxTokensRow: FormGroup | undefined;
  for (const row of array.controls as FormGroup[]) {
    if (row.get('key')?.value === 'max_tokens') {
      maxTokensRow = row;
      break;
    }
  }
  if (!maxTokensRow) return null;

  // Recompute only the two ceiling keys each pass, preserving the per-row
  // bounds errors paramRowBoundsValidator sets independently.
  const rewrite = (extra: Record<string, true>): void => {
    const existing = { ...(maxTokensRow!.errors ?? {}) };
    delete existing['maxTokensMaxAboveCeiling'];
    delete existing['maxTokensDefaultAboveCeiling'];
    const merged = { ...existing, ...extra };
    maxTokensRow!.setErrors(Object.keys(merged).length > 0 ? merged : null);
  };

  if (!maxTokensRow.get('supported')?.value) {
    rewrite({});
    return null;
  }

  const ceiling = array.parent?.get('maxOutputTokens')?.value;
  if (typeof ceiling !== 'number' || !Number.isFinite(ceiling) || ceiling < 1) {
    rewrite({});
    return null;
  }

  const errors: Record<string, true> = {};
  const max = maxTokensRow.get('max')?.value;
  const def = maxTokensRow.get('defaultValue')?.value;
  if (typeof max === 'number' && max > ceiling) errors['maxTokensMaxAboveCeiling'] = true;
  if (typeof def === 'number' && def > ceiling) errors['maxTokensDefaultAboveCeiling'] = true;
  rewrite(errors);
  return null;
}

/**
 * Helper used as a key on each known-param row so the FormArray validator
 * can find the `thinking` and `max_tokens` rows. Custom rows already store
 * their key in a `key` control; known rows don't, so we tag them with a
 * disabled control of the same name for symmetry.
 */
function knownParamKeyControl(fb: FormBuilder, key: string): FormControl<string> {
  return fb.control(key, { nonNullable: true });
}

interface ModelFormGroup {
  modelId: FormControl<string>;
  modelName: FormControl<string>;
  provider: FormControl<ModelProvider>;
  providerName: FormControl<string>;
  inputModalities: FormControl<string[]>;
  outputModalities: FormControl<string[]>;
  maxInputTokens: FormControl<number>;
  maxOutputTokens: FormControl<number>;
  allowedAppRoles: FormControl<string[]>;
  availableToRoles: FormControl<string[]>;
  enabled: FormControl<boolean>;
  isDefault: FormControl<boolean>;
  inputPricePerMillionTokens: FormControl<number>;
  outputPricePerMillionTokens: FormControl<number>;
  cacheWritePricePerMillionTokens: FormControl<number | null>;
  cacheReadPricePerMillionTokens: FormControl<number | null>;
  knowledgeCutoffDate: FormControl<string | null>;
  supportsCaching: FormControl<boolean>;
  mantleEndpointPath: FormControl<MantleEndpointPath>;
  inferenceParams: FormArray<FormGroup<ParamRowGroup>>;
  customInferenceParams: FormArray<FormGroup<CustomParamRowGroup>>;
}

@Component({
  selector: 'app-model-form-page',
  imports: [ReactiveFormsModule, RouterLink, NgIcon],
  providers: [provideIcons({ heroArrowLeft, heroChevronDown, heroChevronRight })],
  templateUrl: './model-form.page.html',
  styleUrl: './model-form.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ModelFormPage implements OnInit {
  private fb = inject(FormBuilder);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private managedModelsService = inject(ManagedModelsService);
  private prefillService = inject(CuratedModelPrefillService);
  private appRolesService = inject(AppRolesService);

  // Available options for multi-select fields
  readonly availableProviders = AVAILABLE_PROVIDERS;
  readonly availableModalities = ['TEXT', 'IMAGE', 'VIDEO', 'AUDIO', 'SPEECH', 'EMBEDDING'];
  readonly mantleEndpointPaths = MANTLE_ENDPOINT_PATHS;

  /**
   * Tracks the selected provider as a signal so the template can show/hide
   * the Mantle-only endpoint-path field and suppress the caching controls
   * (Mantle open-weight models never cache). Kept in sync with the form
   * control in ngOnInit + its valueChanges subscription.
   */
  readonly selectedProvider = signal<ModelProvider>('bedrock');
  readonly isMantle = computed(() => this.selectedProvider() === 'mantle');

  /**
   * Model-id suggestions for the Mantle escape-hatch form, sourced from the
   * live `GET /admin/mantle/models` roster. The curated cards are the primary
   * path; this just spares an admin adding an off-catalog model from typing
   * an exact id. Fetched once, lazily, the first time the form is on Mantle.
   */
  readonly mantleModelIdOptions = signal<string[]>([]);
  private mantleModelIdsLoaded = false;

  // AppRoles from the API (reactive resource)
  readonly rolesResource = this.appRolesService.rolesResource;
  readonly availableAppRoles = computed(() => this.appRolesService.getEnabledRoles());

  // Form state
  readonly isEditMode = signal<boolean>(false);
  readonly modelId = signal<string | null>(null);
  readonly isSubmitting = signal<boolean>(false);
  readonly isLoading = signal<boolean>(false);

  // Inference-param row metadata, parallel to the ``inferenceParams`` FormArray.
  // Provider switch rebuilds both together so each row is paired with its
  // friendly label and input kind.
  readonly inferenceParamRows = signal<KnownParamMeta[]>([]);

  // Form group
  readonly modelForm: FormGroup<ModelFormGroup> = this.fb.group({
    modelId: this.fb.control('', { nonNullable: true, validators: [Validators.required] }),
    modelName: this.fb.control('', { nonNullable: true, validators: [Validators.required] }),
    provider: this.fb.control<ModelProvider>('bedrock', { nonNullable: true, validators: [Validators.required] }),
    providerName: this.fb.control('', { nonNullable: true, validators: [Validators.required] }),
    inputModalities: this.fb.control<string[]>([], { nonNullable: true, validators: [Validators.required] }),
    outputModalities: this.fb.control<string[]>([], { nonNullable: true, validators: [Validators.required] }),
    maxInputTokens: this.fb.control(0, { nonNullable: true, validators: [Validators.required, Validators.min(1)] }),
    maxOutputTokens: this.fb.control(0, { nonNullable: true, validators: [Validators.required, Validators.min(1)] }),
    allowedAppRoles: this.fb.control<string[]>([], { nonNullable: true, validators: [Validators.required] }),
    availableToRoles: this.fb.control<string[]>([], { nonNullable: true }),
    enabled: this.fb.control(true, { nonNullable: true }),
    isDefault: this.fb.control(false, { nonNullable: true }),
    inputPricePerMillionTokens: this.fb.control(0, { nonNullable: true, validators: [Validators.required, Validators.min(0)] }),
    outputPricePerMillionTokens: this.fb.control(0, { nonNullable: true, validators: [Validators.required, Validators.min(0)] }),
    cacheWritePricePerMillionTokens: this.fb.control<number | null>(null, { validators: [Validators.min(0)] }),
    cacheReadPricePerMillionTokens: this.fb.control<number | null>(null, { validators: [Validators.min(0)] }),
    knowledgeCutoffDate: this.fb.control<string | null>(null),
    supportsCaching: this.fb.control(false, { nonNullable: true }),
    mantleEndpointPath: this.fb.control<MantleEndpointPath>('/v1', { nonNullable: true }),
    inferenceParams: this.fb.array<FormGroup<ParamRowGroup>>([], {
      validators: [thinkingInvariantsValidator, maxTokensCeilingValidator],
    }),
    customInferenceParams: this.fb.array<FormGroup<CustomParamRowGroup>>([]),
  });

  // Bound to the "Add custom parameter" input. Cleared after a successful add.
  readonly newCustomParamKey = signal<string>('');
  readonly newCustomParamError = signal<string | null>(null);

  /**
   * Param keys that came from the persisted record on load. Combined with
   * each row's dirty flag to decide what `collectSupportedParams()` persists:
   * untouched + not-loaded rows are dropped so the admin's "did nothing"
   * means "passthrough", not "block everything".
   */
  private loadedKnownKeys = new Set<string>();

  /**
   * Inference Parameters section visibility. Collapsed by default — most
   * admins won't need to touch this for chat-shaped models, and the runtime
   * already supplies sensible per-model defaults via the seed/registry.
   */
  readonly inferenceParamsExpanded = signal<boolean>(false);

  toggleInferenceParams(): void {
    this.inferenceParamsExpanded.update(v => !v);
  }

  /**
   * Count of parameters the admin has actually configured on this model.
   * Drives the collapsed-section summary so the admin knows at a glance
   * whether anything's set before they expand.
   *
   * Implemented as a method (not computed signal) so it re-evaluates on
   * every CD pass — form-control changes don't tick signal dependencies,
   * and we want the count to track checkbox/key edits in real time.
   */
  configuredParamCount(): number {
    let count = 0;
    this.modelForm.controls.inferenceParams.controls.forEach((row, i) => {
      const meta = this.inferenceParamRows()[i];
      if (!meta) return;
      const wasLoaded = this.loadedKnownKeys.has(meta.key);
      const wasTouched = row.dirty;
      if ((wasLoaded || wasTouched) && row.controls.supported.value) {
        count++;
      }
    });
    this.modelForm.controls.customInferenceParams.controls.forEach(row => {
      if (row.controls.key.value) count++;
    });
    return count;
  }

  readonly pageTitle = computed(() => this.isEditMode() ? 'Edit Model' : 'Add Model');

  ngOnInit(): void {
    // Seed the inference-params section for the default provider before any
    // edit-mode load runs, so it can patch into existing rows.
    this.rebuildInferenceParamRows(this.modelForm.controls.provider.value);

    // Check if we're in edit mode
    const id = this.route.snapshot.paramMap.get('id');
    if (id && id !== 'new') {
      this.isEditMode.set(true);
      this.modelId.set(id);
      this.loadModelData(id);
    } else {
      // Curated catalog handoff: a one-shot template seeded by the catalog
      // page lives in CuratedModelPrefillService. Consume it before falling
      // through to the older query-param prefill so the richer template
      // (pricing + supportedParams) wins when both are present.
      const pending = this.prefillService.consume();
      if (pending) {
        this.prefillFromCuratedTemplate(pending);
      } else {
        // Check for query params (from Bedrock models page)
        const queryParams = this.route.snapshot.queryParams;
        if (queryParams['modelId']) {
          this.prefillFromQueryParams(queryParams);
        }
      }
    }

    // Clear cache pricing when supportsCaching is toggled off
    this.modelForm.controls.supportsCaching.valueChanges.subscribe(supportsCaching => {
      if (!supportsCaching) {
        this.modelForm.patchValue({
          cacheWritePricePerMillionTokens: null,
          cacheReadPricePerMillionTokens: null,
        });
      }
    });

    // Mirror the initial provider into the signal so Mantle-only UI is
    // correct on first paint (edit mode / curated prefill set it before this).
    this.selectedProvider.set(this.modelForm.controls.provider.value);
    if (this.isMantle()) {
      this.loadMantleModelIdOptions();
    }

    // Rebuild the inference-param rows whenever the provider changes so the
    // visible knobs match what the selected SDK actually understands. Also
    // keep the provider signal in sync and lazily pull the Mantle model-id
    // suggestions the first time the form lands on Mantle.
    this.modelForm.controls.provider.valueChanges.subscribe(provider => {
      this.rebuildInferenceParamRows(provider);
      this.selectedProvider.set(provider);
      if (provider === 'mantle') {
        this.loadMantleModelIdOptions();
      }
    });

    // Keep the max_tokens row pinned to the model's output ceiling: pre-fill
    // it on a fresh model and re-check the cap whenever the ceiling changes.
    this.modelForm.controls.maxOutputTokens.valueChanges.subscribe(() => {
      this.syncMaxTokensCeiling();
    });
  }

  /**
   * Build (or rebuild) the inference-params FormArray for a given provider.
   *
   * When the param key list is unchanged we **patch values in place** rather
   * than clear+push. Replacing FormGroup instances is unsafe here because the
   * already-rendered ``[formGroupName]="i"`` / ``formControlName="..."``
   * directives bind once and don't notice when a parent FormArray's child
   * gets swapped — clicks would update the old, detached FormGroup and the
   * ``@if (paramRowGroup(i).controls.supported.value)`` guard would never
   * flip. A full rebuild only fires when the visible row set actually
   * changes (i.e. real provider switch).
   *
   * Persisted keys outside the known catalog populate the custom-params
   * FormArray instead — admins can stage params the frontend catalog doesn't
   * yet recognize without losing them on save.
   */
  private rebuildInferenceParamRows(provider: ModelProvider, existing?: SupportedParams | null): void {
    const knownForProvider = KNOWN_PARAMS.filter(p => p.providers.includes(provider));
    const knownKeys = new Set(KNOWN_PARAMS.map(p => p.key));

    // Track which keys came from the persisted record so the save path can
    // round-trip them even if the admin doesn't interact with the row.
    if (existing?.params) {
      for (const k of Object.keys(existing.params)) {
        this.loadedKnownKeys.add(k);
      }
    }

    const arr = this.modelForm.controls.inferenceParams;
    const currentMetas = this.inferenceParamRows();
    const sameStructure =
      currentMetas.length === knownForProvider.length &&
      currentMetas.every((m, i) => m.key === knownForProvider[i].key);

    if (sameStructure) {
      // Patch persisted values into existing rows. Where there's no override,
      // leave the seeded defaults alone so admins keep their suggested bounds.
      knownForProvider.forEach((meta, i) => {
        const row = arr.at(i) as FormGroup<ParamRowGroup>;
        const fromExisting = existing?.params?.[meta.key];
        if (!fromExisting) return;
        row.patchValue({
          supported: fromExisting.supported,
          min: fromExisting.min ?? row.controls.min.value,
          max: fromExisting.max ?? row.controls.max.value,
          allowed: fromExisting.allowed ?? row.controls.allowed.value,
          defaultValue: fromExisting.default ?? null,
          locked: fromExisting.locked,
        });
      });
    } else {
      // Snapshot in-flight values keyed by param name so survivors of the
      // provider switch keep what the admin typed.
      const currentValues = new Map<string, ModelParamSpec>();
      arr.controls.forEach((row, i) => {
        const key = currentMetas[i]?.key;
        if (key) {
          currentValues.set(key, this.rowToSpec(row));
        }
      });
      arr.clear();
      for (const meta of knownForProvider) {
        const fromExisting = existing?.params?.[meta.key];
        const fromCurrent = currentValues.get(meta.key);
        const seed = fromExisting ?? fromCurrent ?? null;
        arr.push(this.buildParamRow(meta, seed, provider));
      }
      this.inferenceParamRows.set(knownForProvider);
    }

    // Custom params: patch in place when key set matches, full rebuild
    // otherwise (same FormGroup-identity reasoning as the known section).
    const customArr = this.modelForm.controls.customInferenceParams;
    const persistedCustomKeys = Object.keys(existing?.params ?? {}).filter(k => !knownKeys.has(k));
    const currentCustomKeys = customArr.controls.map(r => r.controls.key.value);

    const sameCustomStructure =
      persistedCustomKeys.length === currentCustomKeys.length &&
      persistedCustomKeys.every((k, i) => k === currentCustomKeys[i]);

    if (sameCustomStructure && persistedCustomKeys.length > 0) {
      persistedCustomKeys.forEach((key, i) => {
        const row = customArr.at(i) as FormGroup<CustomParamRowGroup>;
        const spec = existing!.params![key];
        row.patchValue({
          key,
          supported: spec.supported,
          min: spec.min ?? row.controls.min.value,
          max: spec.max ?? row.controls.max.value,
          allowed: spec.allowed ?? row.controls.allowed.value,
          defaultValue: spec.default ?? null,
          locked: spec.locked,
        });
      });
    } else if (persistedCustomKeys.length > 0 || currentCustomKeys.length > 0) {
      // Preserve in-flight custom edits that aren't in the persisted record.
      const currentCustom = new Map<string, ModelParamSpec>();
      customArr.controls.forEach(row => {
        const v = row.getRawValue();
        if (v.key) {
          currentCustom.set(v.key, this.rowToSpec(row as unknown as FormGroup<ParamRowGroup>));
        }
      });
      customArr.clear();
      const seen = new Set<string>();
      for (const key of persistedCustomKeys) {
        seen.add(key);
        customArr.push(this.buildCustomParamRow(key, existing!.params![key]));
      }
      currentCustom.forEach((spec, key) => {
        if (!seen.has(key)) {
          customArr.push(this.buildCustomParamRow(key, spec));
        }
      });
    }

    this.syncMaxTokensCeiling();
  }

  /**
   * Pin the `max_tokens` inference-param row to the model's declared output
   * ceiling. Pre-fills the row's Max and Default from `maxOutputTokens` so a
   * fresh model defaults to "request the full ceiling" — but only while
   * those fields are untouched and weren't loaded from a persisted record,
   * so deliberate admin edits and saved specs win. Always re-validates the
   * array so the ceiling cap re-checks when only the model-level field
   * changed (a sibling value change doesn't re-run the array validator on
   * its own).
   */
  private syncMaxTokensCeiling(): void {
    const idx = this.inferenceParamRows().findIndex(m => m.key === 'max_tokens');
    if (idx < 0) return;
    const row = this.paramRowGroup(idx);
    const ceiling = this.modelForm.controls.maxOutputTokens.value;
    const loaded = this.loadedKnownKeys.has('max_tokens');

    if (!loaded && typeof ceiling === 'number' && Number.isFinite(ceiling) && ceiling >= 1) {
      if (row.controls.max.pristine) {
        row.controls.max.setValue(ceiling, { emitEvent: false });
      }
      if (row.controls.defaultValue.pristine) {
        row.controls.defaultValue.setValue(ceiling, { emitEvent: false });
      }
    }

    this.modelForm.controls.inferenceParams.updateValueAndValidity();
  }

  private buildCustomParamRow(key: string, seed: ModelParamSpec | null): FormGroup<CustomParamRowGroup> {
    return this.fb.group<CustomParamRowGroup>(
      {
        key: this.fb.control(key, { nonNullable: true, validators: [Validators.required, Validators.pattern(CUSTOM_PARAM_KEY_PATTERN)] }),
        supported: this.fb.control(seed?.supported ?? true, { nonNullable: true }),
        min: this.fb.control<number | null>(seed?.min ?? null),
        max: this.fb.control<number | null>(seed?.max ?? null),
        // Custom rows have no catalog kind, so they're never enum-select;
        // round-trip a persisted `allowed` if one was stored, else null.
        allowed: this.fb.control<(string | number)[] | null>(seed?.allowed ?? null),
        defaultValue: this.fb.control<number | boolean | string | null>(seed?.default ?? null),
        locked: this.fb.control(seed?.locked ?? false, { nonNullable: true }),
      },
      { validators: [paramRowBoundsValidator] },
    );
  }

  /** Push a new custom-param row from the "Add custom parameter" input. */
  addCustomParam(): void {
    const raw = this.newCustomParamKey().trim();
    if (!raw) {
      this.newCustomParamError.set('Enter a parameter key.');
      return;
    }
    if (!CUSTOM_PARAM_KEY_PATTERN.test(raw)) {
      this.newCustomParamError.set('Use snake_case: lowercase letters, digits, and underscores.');
      return;
    }
    const knownKeys = new Set(KNOWN_PARAMS.map(p => p.key));
    if (knownKeys.has(raw)) {
      this.newCustomParamError.set(`'${raw}' is a built-in parameter — toggle it on above instead.`);
      return;
    }
    const existingCustomKeys = this.modelForm.controls.customInferenceParams.controls
      .map(r => r.controls.key.value);
    if (existingCustomKeys.includes(raw)) {
      this.newCustomParamError.set(`'${raw}' is already in the list.`);
      return;
    }
    this.modelForm.controls.customInferenceParams.push(this.buildCustomParamRow(raw, null));
    this.newCustomParamKey.set('');
    this.newCustomParamError.set(null);
  }

  removeCustomParam(index: number): void {
    this.modelForm.controls.customInferenceParams.removeAt(index);
  }

  customParamGroup(index: number): FormGroup<CustomParamRowGroup> {
    return this.modelForm.controls.customInferenceParams.at(index) as FormGroup<CustomParamRowGroup>;
  }

  onCustomKeyInput(value: string): void {
    this.newCustomParamKey.set(value);
    if (this.newCustomParamError()) {
      this.newCustomParamError.set(null);
    }
  }

  private buildParamRow(meta: KnownParamMeta, seed: ModelParamSpec | null, provider: ModelProvider): FormGroup<ParamRowGroup> {
    // Per-provider seeded bounds win over the catalog-wide fallbacks. Persisted
    // values from `seed` always win over both — admin edits aren't clobbered.
    const providerBounds = meta.defaults?.[provider];
    const seedMin = seed?.min ?? providerBounds?.min ?? meta.defaultMin ?? null;
    const seedMax = seed?.max ?? providerBounds?.max ?? meta.defaultMax ?? null;
    // Enum-select rows (e.g. effort) carry an `allowed` subset instead of
    // min/max. Empty array on a fresh row marks it as "select kind" for the
    // validator/template and forces the admin to opt into levels explicitly.
    const seedAllowed: (string | number)[] | null =
      meta.kind === 'select' ? (seed?.allowed ?? []) : null;
    return this.fb.group<ParamRowGroup>(
      {
        // Catalog key is fixed for known rows — no validators, just a read-only
        // tag the FormArray validator can pivot on.
        key: knownParamKeyControl(this.fb, meta.key),
        supported: this.fb.control(seed?.supported ?? false, { nonNullable: true }),
        min: this.fb.control<number | null>(seedMin),
        max: this.fb.control<number | null>(seedMax),
        allowed: this.fb.control<(string | number)[] | null>(seedAllowed),
        defaultValue: this.fb.control<number | boolean | string | null>(seed?.default ?? null),
        locked: this.fb.control(seed?.locked ?? false, { nonNullable: true }),
      },
      { validators: [paramRowBoundsValidator] },
    );
  }

  private rowToSpec(row: FormGroup<ParamRowGroup>): ModelParamSpec {
    const v = row.getRawValue();
    return {
      supported: v.supported,
      min: v.min,
      max: v.max,
      allowed: v.allowed,
      default: v.defaultValue,
      locked: v.locked,
    };
  }

  /**
   * Convert the inference-params FormArrays + row metadata into the canonical
   * `SupportedParams` shape the API expects.
   *
   * Known-param rows are only persisted when the admin has opined on them —
   * either by loading from a persisted record (tracked in
   * ``loadedKnownKeys``) or by interacting with the row in this session
   * (FormGroup ``dirty``). Untouched rows are omitted, so the runtime sees
   * an empty spec map and falls back to passthrough. That keeps "I didn't
   * touch this section" symmetric for new vs existing models — neither
   * silently flips behavior.
   *
   * Custom rows are always explicit (admin had to type a key or load one
   * from DDB) so they're persisted whenever present.
   */
  private collectSupportedParams(): SupportedParams | null {
    const rows = this.inferenceParamRows();
    const params: Record<string, ModelParamSpec> = {};

    this.modelForm.controls.inferenceParams.controls.forEach((row, i) => {
      const meta = rows[i];
      if (!meta) return;
      const wasLoaded = this.loadedKnownKeys.has(meta.key);
      const wasTouched = row.dirty;
      if (!wasLoaded && !wasTouched) return;
      params[meta.key] = this.rowToSpec(row);
    });

    this.modelForm.controls.customInferenceParams.controls.forEach(row => {
      const v = row.getRawValue();
      const key = (v.key ?? '').trim();
      if (!key) return;
      params[key] = this.rowToSpec(row as unknown as FormGroup<ParamRowGroup>);
    });

    return Object.keys(params).length > 0 ? { params } : null;
  }

  paramRowGroup(index: number): FormGroup<ParamRowGroup> {
    return this.modelForm.controls.inferenceParams.at(index) as FormGroup<ParamRowGroup>;
  }

  /**
   * Returns the inline error messages to render under a known-param row, or
   * `[]` for none. Errors come from two sources: per-row bounds checks
   * (`paramRowBoundsValidator`) and thinking-specific cross-row checks
   * (`thinkingInvariantsValidator` writes onto the thinking row).
   *
   * Surfaced unconditionally for supported rows — bounds problems are
   * always immediate and don't need a `touched` gate to feel right.
   */
  paramRowErrors(index: number, kind: 'known' | 'custom' = 'known'): string[] {
    const arr = kind === 'known'
      ? this.modelForm.controls.inferenceParams
      : this.modelForm.controls.customInferenceParams;
    const row = arr.at(index) as FormGroup<ParamRowGroup> | undefined;
    if (!row || !row.errors || !row.get('supported')?.value) return [];
    const out: string[] = [];
    if (row.errors['minGreaterThanMax']) out.push('Min must be less than or equal to Max.');
    if (row.errors['defaultBelowMin']) out.push('Default must be greater than or equal to Min.');
    if (row.errors['defaultAboveMax']) out.push('Default must be less than or equal to Max.');
    if (row.errors['thinkingBudgetTooLow']) out.push('Thinking budget must be at least 1024 tokens.');
    if (row.errors['thinkingBudgetExceedsMaxTokens']) {
      out.push('Thinking budget must be less than the Max Output Tokens default.');
    }
    if (row.errors['thinkingBudgetNotNumeric']) {
      out.push('Thinking budget must be a number — clear the value to disable, or enter an integer ≥ 1024.');
    }
    if (row.errors['maxTokensMaxAboveCeiling'] || row.errors['maxTokensDefaultAboveCeiling']) {
      const ceiling = this.modelForm.controls.maxOutputTokens.value;
      if (row.errors['maxTokensMaxAboveCeiling']) {
        out.push(`Max must be ≤ the model's Max Output Tokens (${ceiling}).`);
      }
      if (row.errors['maxTokensDefaultAboveCeiling']) {
        out.push(`Default must be ≤ the model's Max Output Tokens (${ceiling}).`);
      }
    }
    if (row.errors['allowedEmpty']) {
      out.push('Select at least one level this model supports.');
    }
    if (row.errors['defaultNotAllowed']) {
      out.push('Default must be one of the selected levels.');
    }
    return out;
  }

  /**
   * Whether `value` is in the enum-select row's `allowed` subset. Backs the
   * per-level checkboxes for `kind: 'select'` params (e.g. effort).
   */
  isParamAllowed(index: number, value: string): boolean {
    return (this.paramRowGroup(index).controls.allowed.value ?? []).includes(value);
  }

  /**
   * Toggle a level in the enum-select row's `allowed` subset. Clears the
   * row default if the level backing it was just removed so the
   * default-in-allowed invariant can't be left stale.
   */
  toggleParamAllowed(index: number, value: string): void {
    const row = this.paramRowGroup(index);
    const current = row.controls.allowed.value ?? [];
    const next = current.includes(value)
      ? current.filter(v => v !== value)
      : [...current, value];
    row.controls.allowed.setValue(next);
    row.controls.allowed.markAsDirty();
    if (row.controls.defaultValue.value != null && !next.includes(row.controls.defaultValue.value as string)) {
      row.controls.defaultValue.setValue(null);
    }
    row.controls.allowed.updateValueAndValidity();
  }

  /**
   * Load model data for editing
   */
  private async loadModelData(id: string): Promise<void> {
    this.isLoading.set(true);
    try {
      const model = await this.managedModelsService.getModel(id);

      // Populate form with model data
      this.modelForm.patchValue({
        modelId: model.modelId,
        modelName: model.modelName,
        provider: model.provider as ModelProvider,
        providerName: model.providerName,
        inputModalities: model.inputModalities.map(m => m.toUpperCase()),
        outputModalities: model.outputModalities.map(m => m.toUpperCase()),
        maxInputTokens: model.maxInputTokens,
        maxOutputTokens: model.maxOutputTokens,
        allowedAppRoles: model.allowedAppRoles ?? [],
        availableToRoles: model.availableToRoles ?? [],
        enabled: model.enabled,
        isDefault: model.isDefault ?? false,
        inputPricePerMillionTokens: model.inputPricePerMillionTokens,
        outputPricePerMillionTokens: model.outputPricePerMillionTokens,
        cacheWritePricePerMillionTokens: model.cacheWritePricePerMillionTokens ?? null,
        cacheReadPricePerMillionTokens: model.cacheReadPricePerMillionTokens ?? null,
        knowledgeCutoffDate: model.knowledgeCutoffDate,
        supportsCaching: model.supportsCaching ?? true,
        mantleEndpointPath: this.coerceMantlePath(model.mantleEndpointPath),
      });

      // Repopulate the inference-params rows with any persisted spec.
      this.rebuildInferenceParamRows(model.provider as ModelProvider, model.supportedParams ?? null);
    } catch (error) {
      console.error('Error loading model data:', error);
      alert('Failed to load model data. Please try again.');
      this.router.navigate(['/admin/manage-models']);
    } finally {
      this.isLoading.set(false);
    }
  }

  /**
   * Apply a curated template to the form. Mirrors `loadModelData`'s patching
   * shape, so the admin sees a fully-populated form they can review and tweak
   * before clicking Create. Patches main fields first so the provider
   * valueChanges fires and rebuilds the inference-params rows; then re-runs
   * `rebuildInferenceParamRows` with the template's `supportedParams` so the
   * per-param bounds/defaults land in those rows.
   */
  private prefillFromCuratedTemplate(template: ManagedModelFormData): void {
    this.modelForm.patchValue({
      modelId: template.modelId,
      modelName: template.modelName,
      provider: template.provider,
      providerName: template.providerName,
      inputModalities: template.inputModalities.map(m => m.toUpperCase()),
      outputModalities: template.outputModalities.map(m => m.toUpperCase()),
      maxInputTokens: template.maxInputTokens,
      maxOutputTokens: template.maxOutputTokens,
      allowedAppRoles: template.allowedAppRoles ?? [],
      availableToRoles: template.availableToRoles ?? [],
      enabled: template.enabled,
      isDefault: template.isDefault,
      inputPricePerMillionTokens: template.inputPricePerMillionTokens,
      outputPricePerMillionTokens: template.outputPricePerMillionTokens,
      cacheWritePricePerMillionTokens: template.cacheWritePricePerMillionTokens ?? null,
      cacheReadPricePerMillionTokens: template.cacheReadPricePerMillionTokens ?? null,
      knowledgeCutoffDate: template.knowledgeCutoffDate ?? null,
      supportsCaching: template.supportsCaching ?? true,
      mantleEndpointPath: this.coerceMantlePath(template.mantleEndpointPath),
    });

    this.rebuildInferenceParamRows(template.provider, template.supportedParams ?? null);
  }

  /**
   * Prefill form from query parameters (from Bedrock models page)
   */
  private prefillFromQueryParams(params: any): void {
    if (params['modelId']) {
      this.modelForm.patchValue({
        modelId: params['modelId'] || '',
        modelName: params['modelName'] || '',
        provider: params['provider'] || 'bedrock',
        providerName: params['providerName'] || '',
        inputModalities: params['inputModalities'] ? params['inputModalities'].split(',') : [],
        outputModalities: params['outputModalities'] ? params['outputModalities'].split(',') : [],
        maxInputTokens: params['maxInputTokens'] ? parseInt(params['maxInputTokens'], 10) : 0,
        maxOutputTokens: params['maxOutputTokens'] ? parseInt(params['maxOutputTokens'], 10) : 0,
        knowledgeCutoffDate: params['knowledgeCutoffDate'] || null,
      });
    }
  }

  /**
   * Fetch the live Bedrock Mantle roster once to seed the model-id datalist
   * for the escape-hatch form. Best-effort: failures leave the datalist empty
   * (the admin can still type any id), so we swallow errors quietly.
   */
  private async loadMantleModelIdOptions(): Promise<void> {
    if (this.mantleModelIdsLoaded) return;
    this.mantleModelIdsLoaded = true;
    try {
      const response = await this.managedModelsService.fetchMantleModels();
      this.mantleModelIdOptions.set(response.models.map(m => m.id));
    } catch {
      // Non-fatal — the datalist is a convenience, not a requirement.
      this.mantleModelIdsLoaded = false;
    }
  }

  /**
   * Toggle a value in a multi-select array
   */
  toggleArrayValue(controlName: keyof ModelFormGroup, value: string): void {
    const control = this.modelForm.get(controlName) as FormControl<string[]>;
    const currentValue = control.value || [];

    if (currentValue.includes(value)) {
      control.setValue(currentValue.filter(v => v !== value));
    } else {
      control.setValue([...currentValue, value]);
    }
  }

  /**
   * Check if a value is selected in a multi-select array
   */
  isSelected(controlName: keyof ModelFormGroup, value: string): boolean {
    const control = this.modelForm.get(controlName) as FormControl<string[]>;
    return control.value?.includes(value) ?? false;
  }

  /**
   * Submit the form
   */
  async onSubmit(): Promise<void> {
    if (this.modelForm.invalid) {
      this.modelForm.markAllAsTouched();
      return;
    }

    this.isSubmitting.set(true);

    try {
      // The FormArray for inference params lives outside the flat form-value
      // shape that ManagedModelFormData expects, so we read fields directly
      // from the typed form controls and attach `supportedParams` separately.
      const v = this.modelForm.getRawValue();
      const formData: ManagedModelFormData = {
        modelId: v.modelId,
        modelName: v.modelName,
        provider: v.provider,
        providerName: v.providerName,
        inputModalities: v.inputModalities,
        outputModalities: v.outputModalities,
        responseStreamingSupported: false,
        maxInputTokens: v.maxInputTokens,
        maxOutputTokens: v.maxOutputTokens,
        allowedAppRoles: v.allowedAppRoles,
        availableToRoles: v.availableToRoles,
        enabled: v.enabled,
        isDefault: v.isDefault,
        inputPricePerMillionTokens: v.inputPricePerMillionTokens,
        outputPricePerMillionTokens: v.outputPricePerMillionTokens,
        cacheWritePricePerMillionTokens: v.cacheWritePricePerMillionTokens,
        cacheReadPricePerMillionTokens: v.cacheReadPricePerMillionTokens,
        knowledgeCutoffDate: v.knowledgeCutoffDate,
        supportsCaching: v.supportsCaching,
        // Only meaningful for Mantle; null elsewhere so the backend stores
        // nothing for other providers.
        mantleEndpointPath: v.provider === 'mantle' ? v.mantleEndpointPath : null,
        supportedParams: this.collectSupportedParams(),
      };

      if (this.isEditMode() && this.modelId()) {
        // Update existing model
        await this.managedModelsService.updateModel(this.modelId()!, formData);
      } else {
        // Create new model
        await this.managedModelsService.createModel(formData);
      }

      // Navigate back to manage models page
      this.router.navigate(['/admin/manage-models']);
    } catch (error: any) {
      console.error('Error saving model:', error);

      // Extract error message if available
      const errorMessage = error?.error?.detail || error?.message || 'Failed to save model. Please try again.';
      alert(errorMessage);
    } finally {
      this.isSubmitting.set(false);
    }
  }

  /**
   * Normalize a stored/templated Mantle path onto the known options, falling
   * back to the default `/v1` for null/legacy/unknown values so the select
   * always has a valid selection.
   */
  private coerceMantlePath(value: string | null | undefined): MantleEndpointPath {
    return MANTLE_ENDPOINT_PATHS.includes(value as MantleEndpointPath)
      ? (value as MantleEndpointPath)
      : '/v1';
  }

  /**
   * Cancel and navigate back
   */
  onCancel(): void {
    this.router.navigate(['/admin/manage-models']);
  }
}
