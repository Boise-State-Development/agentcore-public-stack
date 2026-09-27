import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { Subject } from 'rxjs';
import { ModelCatalogPage } from './model-catalog.page';
import { ManagedModelsService } from './services/managed-models.service';
import { CuratedModelPrefillService } from './services/curated-model-prefill.service';
import { AddCuratedModelDialogComponent } from './components/add-curated-model-dialog.component';
import {
  CURATED_BEDROCK_MODELS,
  CURATED_BEDROCK_RESPONSES_MODELS,
  CURATED_MANTLE_MODELS,
} from './models/curated-models';

function createMockManagedModelsService(overrides: Partial<{
  isModelAdded: (modelId: string) => boolean;
  createModel: ReturnType<typeof vi.fn>;
}> = {}) {
  return {
    isModelAdded: overrides.isModelAdded ?? (() => false),
    createModel: overrides.createModel ?? vi.fn().mockResolvedValue({ id: 'created' }),
  };
}

function createMockPrefillService() {
  return {
    set: vi.fn(),
    consume: vi.fn().mockReturnValue(null),
  };
}

/**
 * Mock CDK Dialog: each call to `open()` returns a dialogRef whose `closed`
 * observable can be resolved imperatively in the test via the returned
 * `resolve()` helper.
 */
function createMockDialog() {
  const opened: Array<{ component: unknown; data: unknown; closed: Subject<unknown> }> = [];
  const open = vi.fn((component: unknown, config: { data: unknown }) => {
    const closed = new Subject<unknown>();
    opened.push({ component, data: config.data, closed });
    return { closed };
  });
  const lastOpened = () => opened[opened.length - 1];
  const resolveLast = (value: unknown) => {
    const last = lastOpened();
    last.closed.next(value);
    last.closed.complete();
  };
  return { open, opened, lastOpened, resolveLast };
}

describe('ModelCatalogPage', () => {
  let mockService: ReturnType<typeof createMockManagedModelsService>;
  let mockPrefill: ReturnType<typeof createMockPrefillService>;
  let mockDialog: ReturnType<typeof createMockDialog>;
  let routerNavigate: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    mockService = createMockManagedModelsService();
    mockPrefill = createMockPrefillService();
    mockDialog = createMockDialog();
    routerNavigate = vi.fn().mockResolvedValue(true);

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: ManagedModelsService, useValue: mockService },
        { provide: CuratedModelPrefillService, useValue: mockPrefill },
        { provide: Dialog, useValue: mockDialog },
      ],
    });
    TestBed.overrideComponent(ModelCatalogPage, {
      set: { template: '<div></div>' },
    });
    TestBed.overrideProvider(Router, { useValue: { navigate: routerNavigate } });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  function createComponent() {
    const fixture = TestBed.createComponent(ModelCatalogPage);
    fixture.detectChanges();
    return fixture.componentInstance;
  }

  it('defaults to the Bedrock tab and renders the curated entries', () => {
    const page = createComponent();
    expect(page.activeTab()).toBe('bedrock');
    expect(page.visibleModels().map(m => m.key)).toEqual(
      CURATED_BEDROCK_MODELS.map(m => m.key),
    );
  });

  it('shows an empty list when switching to OpenAI or Gemini (Coming soon state)', () => {
    const page = createComponent();
    page.selectTab('openai');
    expect(page.visibleModels()).toEqual([]);
    page.selectTab('gemini');
    expect(page.visibleModels()).toEqual([]);
  });

  it('Preview & customize hands the template to the prefill service and navigates to the form', () => {
    const page = createComponent();
    const target = CURATED_BEDROCK_MODELS[0];

    page.previewCuratedModel(target);

    expect(mockPrefill.set).toHaveBeenCalledWith(target.template);
    expect(routerNavigate).toHaveBeenCalledWith(['/admin/manage-models/new']);
    expect(mockService.createModel).not.toHaveBeenCalled();
  });

  it('addCuratedModel opens the role-picker dialog with the model in data', () => {
    const page = createComponent();
    const target = CURATED_BEDROCK_MODELS[0];

    page.addCuratedModel(target);

    expect(mockDialog.open).toHaveBeenCalledTimes(1);
    expect(mockDialog.opened[0].component).toBe(AddCuratedModelDialogComponent);
    expect(mockDialog.opened[0].data).toEqual({ model: target });
  });

  it('POSTs the template with selected roles when the dialog resolves with role IDs', async () => {
    const page = createComponent();
    const target = CURATED_BEDROCK_MODELS[0];

    const pending = page.addCuratedModel(target);
    mockDialog.resolveLast(['role-user', 'role-admin']);
    await pending;

    expect(mockService.createModel).toHaveBeenCalledWith({
      ...target.template,
      allowedAppRoles: ['role-user', 'role-admin'],
    });
    expect(routerNavigate).toHaveBeenCalledWith(['/admin/manage-models']);
    expect(page.addingKey()).toBeNull();
  });

  it('does not POST when the dialog is cancelled', async () => {
    const page = createComponent();
    const target = CURATED_BEDROCK_MODELS[0];

    const pending = page.addCuratedModel(target);
    mockDialog.resolveLast(undefined);
    await pending;

    expect(mockService.createModel).not.toHaveBeenCalled();
    expect(routerNavigate).not.toHaveBeenCalled();
  });

  it('marks a model as already added when the service reports it exists', () => {
    const existingId = CURATED_BEDROCK_MODELS[0].template.modelId;
    mockService = createMockManagedModelsService({
      isModelAdded: (id) => id === existingId,
    });
    TestBed.overrideProvider(ManagedModelsService, { useValue: mockService });

    const page = createComponent();
    expect(page.isAlreadyAdded(existingId)).toBe(true);
    expect(page.isAlreadyAdded(CURATED_BEDROCK_MODELS[1].template.modelId)).toBe(false);
  });

  it('does not open the dialog when the model is already in the managed list', () => {
    const target = CURATED_BEDROCK_MODELS[0];
    mockService = createMockManagedModelsService({
      isModelAdded: (id) => id === target.template.modelId,
    });
    TestBed.overrideProvider(ManagedModelsService, { useValue: mockService });

    const page = createComponent();
    page.addCuratedModel(target);
    expect(mockDialog.open).not.toHaveBeenCalled();
  });

  it('surfaces backend error.detail inline on the card without navigating', async () => {
    const failure = Object.assign(new Error('http failed'), {
      error: { detail: 'Model ID already in use' },
    });
    mockService = createMockManagedModelsService({
      createModel: vi.fn().mockRejectedValue(failure),
    });
    TestBed.overrideProvider(ManagedModelsService, { useValue: mockService });

    const page = createComponent();
    const target = CURATED_BEDROCK_MODELS[0];

    const pending = page.addCuratedModel(target);
    mockDialog.resolveLast(['role-user']);
    await pending;

    expect(page.errorFor(target.key)).toBe('Model ID already in use');
    expect(routerNavigate).not.toHaveBeenCalled();
    expect(page.addingKey()).toBeNull();
  });

  it('renders curated Mantle cards (with vetted API surface) on the Mantle tab', () => {
    const page = createComponent();
    page.selectTab('mantle');

    const keys = page.visibleModels().map(m => m.key);
    expect(keys).toEqual(CURATED_MANTLE_MODELS.map(m => m.key));

    // The Qwen coder speaks Chat Completions.
    const qwen = page.visibleModels().find(m => m.key === 'qwen3-coder-30b');
    expect(qwen?.template.apiMode).toBe('chat');
    // Mantle models never cache (model-bound to Claude/Nova).
    expect(qwen?.template.supportsCaching).toBe(false);
  });

  it('ignores a second addCuratedModel while a create is in flight', async () => {
    let resolveCreate: (value: unknown) => void = () => {};
    const createPromise = new Promise(res => { resolveCreate = res; });
    mockService = createMockManagedModelsService({
      createModel: vi.fn().mockReturnValue(createPromise),
    });
    TestBed.overrideProvider(ManagedModelsService, { useValue: mockService });

    const page = createComponent();
    const [first, second] = CURATED_BEDROCK_MODELS;

    const inFlight = page.addCuratedModel(first);
    mockDialog.resolveLast(['role-user']);
    // Wait for the dialog promise + into the createModel call before issuing the second.
    await Promise.resolve();
    await Promise.resolve();

    page.addCuratedModel(second);
    expect(mockDialog.open).toHaveBeenCalledTimes(1);

    resolveCreate({ id: 'created' });
    await inFlight;
    expect(page.addingKey()).toBeNull();
  });

  // These guard a mismatch that shipped and stayed live for months: every
  // curated Claude template declared the `global.*` rates while its `modelId`
  // named a `us.*` (Regional/CRIS) inference profile, which prices ~10% higher.
  // Nothing failed — the numbers were merely wrong, everywhere downstream.
  describe('curated Bedrock pricing', () => {
    // The CRIS tier a model id resolves to drives its rate card, so the two
    // must agree on every list that declares a tier — not just Bedrock's.
    const tieredModels = [...CURATED_BEDROCK_MODELS, ...CURATED_BEDROCK_RESPONSES_MODELS];

    it('declares a pricingTier that matches the tier its modelId names', () => {
      for (const model of tieredModels) {
        const expected = model.template.modelId.startsWith('global.') ? 'global' : 'regional';
        expect(`${model.key}:${model.pricingTier}`).toBe(`${model.key}:${expected}`);
      }
    });

    it('derives cache rates from base input at Bedrock\'s published multipliers', () => {
      // Published exceptions to the 1.25x write / 0.1x read default, each from
      // a source outside this file. Anything else that drifts is a typo.
      //  - Claude Opus 5.5 reads at 0.05x: Price List API, $0.22 against $4.40
      //    Regional ($0.20 against $4.00 Global), 2026-09-25.
      //  - GPT-5.5 has no write bucket: its card's cache-write cell is an em
      //    dash, and a live probe reported `cache_write_tokens: 0` on every turn
      //    while reading the prefix back from cache.
      const WRITE_MULTIPLIER: Record<string, number> = { 'gpt-5-5': 0 };
      const READ_MULTIPLIER: Record<string, number> = { 'claude-opus-5-5': 0.05 };
      for (const model of tieredModels) {
        const t = model.template;
        if (!t.supportsCaching) continue;
        const input = t.inputPricePerMillionTokens;
        expect(t.cacheWritePricePerMillionTokens, model.key).toBeCloseTo(
          input * (WRITE_MULTIPLIER[model.key] ?? 1.25),
          6,
        );
        expect(t.cacheReadPricePerMillionTokens, model.key).toBeCloseTo(
          input * (READ_MULTIPLIER[model.key] ?? 0.1),
          6,
        );
      }
    });
  });

  describe('curated bedrock-responses entries', () => {
    // The tab is a TRANSPORT, not a vendor. Three of its invariants below were
    // written when every entry was an `openai.*` id and are measured facts
    // about GPT-5.6 specifically — the 272K price boundary and the probed
    // parameter spec. Kimi K3 shares the transport and none of those facts, so
    // they are scoped here rather than silently widened to a model they were
    // never measured against.
    const OPENAI_FAMILY = CURATED_BEDROCK_RESPONSES_MODELS.filter(m =>
      m.template.modelId.includes('.openai.'),
    );
    const NON_OPENAI = CURATED_BEDROCK_RESPONSES_MODELS.filter(
      m => !m.template.modelId.includes('.openai.'),
    );

    it('keeps both families on the tab', () => {
      expect(OPENAI_FAMILY.length).toBeGreaterThan(0);
      expect(NON_OPENAI.map(m => m.key)).toEqual(['kimi-k3']);
    });

    it('renders them on their own tab', () => {
      const page = createComponent();
      page.selectTab('bedrock-responses');

      expect(page.visibleModels().map(m => m.key)).toEqual(
        CURATED_BEDROCK_RESPONSES_MODELS.map(m => m.key),
      );
      expect(CURATED_BEDROCK_RESPONSES_MODELS.length).toBeGreaterThan(0);
    });

    it('never ships supportsCaching false — the provider forces it true', () => {
      // `false` here is not a preference but a false statement: these models
      // cache implicitly server-side and it cannot be turned off. Its only
      // effect would be to clear the cache rates, pricing cached tokens at
      // $0.00 while AWS bills them in full.
      for (const model of CURATED_BEDROCK_RESPONSES_MODELS) {
        expect(`${model.key}:${model.template.supportsCaching}`).toBe(`${model.key}:true`);
      }
    });

    it('pins the OpenAI family to the 272K short-context boundary', () => {
      // Load-bearing pricing, not just a cap: above 272K these models bill
      // input at 2x and output at 1.5x, and a CuratedModel holds one flat rate
      // per bucket. Raising this silently opens the second price card.
      for (const model of OPENAI_FAMILY) {
        expect(`${model.key}:${model.template.maxInputTokens}`).toBe(`${model.key}:272000`);
      }
    });

    it('lets Kimi K3 use its whole 1M window, because it has one price card', () => {
      // The 272K pin exists to keep ONE flat rate per bucket true where AWS
      // publishes TWO cards. Kimi K3's card publishes a single table per
      // inference option, so there is no second card to fall into and the cap
      // would only force early compaction at no saving. If Moonshot ever adds
      // a long-context card, this is the test that has to change first.
      const kimi = CURATED_BEDROCK_RESPONSES_MODELS.find(m => m.key === 'kimi-k3');

      expect(kimi?.template.maxInputTokens).toBe(1_000_000);
      expect(kimi?.template.providerName).toBe('Moonshot AI');
      // US CRIS column: $3.30 / $16.50. Global is cheaper ($3.00 / $15.00) and
      // is deliberately NOT used: dev sits under Control Tower SCP p-r61tynkc,
      // which denies bedrock:InvokeModel on every `global.*` profile (verified
      // against two Claude ids as well as this one). Curating the Global id
      // would put a model in the picker that 401s on first use.
      expect(kimi?.template.modelId).toBe('us.moonshotai.kimi-k3');
      expect(kimi?.template.inputPricePerMillionTokens).toBeCloseTo(3.3, 6);
      expect(kimi?.template.outputPricePerMillionTokens).toBeCloseTo(16.5, 6);
    });

    it('routes over the Responses API, which is the only surface that caches', () => {
      for (const model of CURATED_BEDROCK_RESPONSES_MODELS) {
        expect(`${model.key}:${model.template.apiMode}`).toBe(`${model.key}:responses`);
        expect(`${model.key}:${model.template.provider}`).toBe(`${model.key}:bedrock-responses`);
      }
    });

    it('declares only the MEASURED supportedParams, never a guessed one', () => {
      // Supersedes an earlier invariant that required NO spec at all. The bar
      // was never "no spec" — it was "no invented spec": a declared spec flips
      // the #915 guard from permissive to restrictive, so a guess silently
      // blocks params the model really accepts. AWS still publishes no
      // parameter table, so this spec comes from probing all four ids in
      // us-west-2 on 2026-09-12 (see the block comment on the array).
      //
      // If a future sibling is added without re-probing, this fails — which is
      // the point.
      for (const model of OPENAI_FAMILY) {
        const params = model.template.supportedParams?.params;
        expect(params, `${model.key} must declare a measured spec`).toBeTruthy();

        // The endpoint's own 400 enumerates exactly these, identically on all four.
        expect(`${model.key}:${params!['reasoning_effort'].allowed?.join(',')}`).toBe(
          `${model.key}:none,low,medium,high,xhigh,max`,
        );

        // Measured 400: "Unsupported parameter: 'temperature' is not supported
        // with this model." Declared false so the request never carries them.
        expect(`${model.key}:${params!['temperature'].supported}`).toBe(`${model.key}:false`);
        expect(`${model.key}:${params!['top_p'].supported}`).toBe(`${model.key}:false`);

        expect(`${model.key}:${params!['max_tokens'].supported}`).toBe(`${model.key}:true`);

        // `medium` pins what the provider was already doing implicitly —
        // measured at ~376 reasoning tokens unset vs ~308 for medium, so this
        // is cost-neutral-to-cheaper rather than an increase. A default must
        // stay a member of `allowed` or the backend drops it.
        expect(`${model.key}:${params!['reasoning_effort'].default}`).toBe(
          `${model.key}:medium`,
        );
        expect(params!['reasoning_effort'].allowed).toContain(
          params!['reasoning_effort'].default,
        );
      }
    });

    it('gives Kimi K3 its OWN measured spec, not the OpenAI family\'s', () => {
      // The whole reason this is a separate test: `openaiResponsesParams`
      // declares `temperature` unsupported, which is a measured fact about
      // GPT-5.6 and FALSE here — probed 2026-09-21 against
      // us.moonshotai.kimi-k3 in us-west-2, temperature is accepted. Borrowing
      // the family spec would have silently stripped a working parameter.
      const kimi = CURATED_BEDROCK_RESPONSES_MODELS.find(m => m.key === 'kimi-k3');
      const params = kimi!.template.supportedParams!.params;

      // 400 on 2: "This model accepts 'temperature' between 0 and 1."
      expect(params['temperature'].supported).toBe(true);
      expect(params['temperature'].min).toBe(0);
      expect(params['temperature'].max).toBe(1);

      // 400 on 0.9: "This model accepts 'top_p' only with the value 0.95."
      // Its sole legal value is what omitting it already gives you, so
      // declaring it supported would only enable a mid-stream 400.
      expect(params['top_p'].supported).toBe(false);

      // 400 on 1: "Expected a value >= 16, but got 1 instead." The family's
      // usual min of 1 would be a guaranteed 400 on this model.
      expect(params['max_tokens'].supported).toBe(true);
      expect(params['max_tokens'].min).toBe(16);

      // Same enum as the OpenAI family, but enumerated independently by this
      // model's own 400 on a bogus level.
      expect(params['reasoning_effort'].allowed?.join(',')).toBe(
        'none,low,medium,high,xhigh,max',
      );
      expect(params['reasoning_effort'].default).toBe('medium');
      expect(params['reasoning_effort'].allowed).toContain(
        params['reasoning_effort'].default,
      );
    });

    it('never lets a non-OpenAI entry inherit the OpenAI parameter spec', () => {
      // A change-detector for the next model added to this transport: the
      // cheap move is to spread `openaiResponsesParams()` and move on, which
      // would declare temperature unsupported on a model that accepts it.
      for (const model of NON_OPENAI) {
        expect(
          model.template.supportedParams?.params['temperature'].supported,
          `${model.key} must declare its OWN temperature support`,
        ).toBe(true);
      }
    });

    it('curates GPT-6 Astra on the Short Context rate card', () => {
      // The 272K cap is covered by the family loop above; this pins the rates
      // that cap keeps correct. Geo CRIS Short Context is $11.00 / $55.00 —
      // Long Context (1.05M) is $22.00 / $82.50, and the tier is chosen by the
      // request's actual token count, so nothing but the cap keeps a single
      // flat rate per bucket true.
      const astra = CURATED_BEDROCK_RESPONSES_MODELS.find(m => m.key === 'gpt-6-astra');

      expect(astra?.template.modelId).toBe('us.openai.gpt-6-astra');
      expect(astra?.template.inputPricePerMillionTokens).toBeCloseTo(11.0, 6);
      expect(astra?.template.outputPricePerMillionTokens).toBeCloseTo(55.0, 6);
    });

    it('declares Astra\'s published output cap and cutoff, which its siblings lack', () => {
      // Astra's card publishes `Max output tokens: 128,000` and an April 30,
      // 2026 cutoff; every GPT-5.6 card states neither, which is why the
      // family default is null for both. Inheriting the default here would
      // discard two numbers AWS actually publishes.
      const astra = CURATED_BEDROCK_RESPONSES_MODELS.find(m => m.key === 'gpt-6-astra');

      expect(astra?.template.maxOutputTokens).toBe(128_000);
      expect(astra?.template.knowledgeCutoffDate).toBe('2026-04-30');

      for (const sibling of CURATED_BEDROCK_RESPONSES_MODELS.filter(m => m.key !== 'gpt-6-astra')) {
        expect(`${sibling.key}:${sibling.template.maxOutputTokens}`).toBe(`${sibling.key}:null`);
      }
    });
  });

  it('curates GPT-5.4 on Mantle with caching on and no write fee', () => {
    // Its model card publishes a cache-read rate with an em dash for cache
    // write. Inheriting mantleDefaults()' supportsCaching:false priced its
    // cached tokens at $0.00 while AWS billed them — the bug that had to be
    // fixed by hand in prod.
    const gpt54 = CURATED_MANTLE_MODELS.find(m => m.key === 'gpt-5-4');

    expect(gpt54?.template.supportsCaching).toBe(true);
    expect(gpt54?.template.cacheReadPricePerMillionTokens).toBeCloseTo(0.275, 6);
    expect(gpt54?.template.cacheWritePricePerMillionTokens).toBe(0);
  });

  describe('curated picker placement', () => {
    const ALL = [
      ...CURATED_BEDROCK_MODELS,
      ...CURATED_MANTLE_MODELS,
      ...CURATED_BEDROCK_RESPONSES_MODELS,
    ];

    // Demoted = superseded by a newer sibling ON THE SAME PROVIDER SURFACE, or
    // specialist enough that it isn't a general chat default. Everything else
    // stays at the picker's top level. This list is a change-detector: adding a
    // model or re-ranking one should be a deliberate edit here, not a drift.
    //
    // "Same surface" is load-bearing. GPT-5.4 (mantle) looks superseded by the
    // GPT-5.6 family until you notice those are bedrock-responses — a different
    // provider an install may not use at all. Demoting it left Mantle with no
    // featured model but a specialist coding one, which the family check below
    // now catches. A template default cannot assume what else gets added.
    const DEMOTED = [
      'claude-opus-4-7',
      'claude-sonnet-4-6',
      'qwen3-coder-30b',
      'gpt-5-5',
      'gpt-5-6-sol',
      'gpt-5-6-terra',
      'gpt-5-6-luna',
    ];

    it('demotes exactly the superseded and specialist models', () => {
      const demoted = ALL.filter(m => m.template.isFeatured === false)
        .map(m => m.key)
        .sort();
      expect(demoted).toEqual([...DEMOTED].sort());
    });

    it('leaves featured models undeclared so they inherit the backend default', () => {
      // `isFeatured` defaults true server-side. Featured rows say nothing
      // rather than `true`, so the default stays in exactly one place.
      for (const model of ALL) {
        if (DEMOTED.includes(model.key)) continue;
        expect(
          model.template.isFeatured,
          `${model.key} should not declare isFeatured`,
        ).toBeUndefined();
      }
    });

    it('keeps a featured model in every provider family', () => {
      // A catalog tab whose every entry is demoted would put an entire
      // provider behind the submenu, which is never the intent.
      for (const [label, group] of [
        ['bedrock', CURATED_BEDROCK_MODELS],
        ['mantle', CURATED_MANTLE_MODELS],
        ['bedrock-responses', CURATED_BEDROCK_RESPONSES_MODELS],
      ] as const) {
        const featured = group.filter(m => m.template.isFeatured !== false);
        expect(featured.length, `${label} must keep a featured model`).toBeGreaterThan(0);
      }
    });

    it('does not let two featured models both claim to be the most capable', () => {
      // GPT-6 Astra outranks (and out-prices) GPT-5.6 Sol in the same catalog,
      // so Sol's copy must not say "most capable".
      const featuredCopy = ALL.filter(m => m.template.isFeatured !== false)
        .map(m => m.template.shortDescription ?? '');
      const superlatives = featuredCopy.filter(d => /most capable/i.test(d));
      expect(superlatives).toEqual([]);
    });
  });
});
