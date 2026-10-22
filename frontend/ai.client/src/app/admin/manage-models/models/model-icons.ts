import { ManagedModel } from './managed-model.model';

/**
 * Built-in logos shipped with the SPA.
 *
 * Each slug has a `public/img/provider-logos/{slug}/{light,dark}.svg` pair. Adding
 * one means dropping the pair in, listing it here, and listing it in the
 * backend's `BUILTIN_MODEL_ICONS` (`apis/shared/models/model_icons.py`), which
 * validates the slug on write — a slug only one side knows is a tile that renders
 * as nothing, with no error anywhere to say why.
 *
 * Two kinds live in this one namespace, and the distinction drives
 * {@link resolveModelIcon}'s precedence rather than the naming:
 *
 * - **Model-family marks** (`claude`, `kimi`, `qwen`) — what the vendor brands
 *   the *models* with. Preferred, because it is what a user recognises next to
 *   "Claude Sonnet 5".
 * - **Company marks** (`anthropic`, `openai`, `amazon`, `meta`, `google`) — the
 *   corporate logo, used when the family has no mark of its own, or has one that
 *   is unusable at this size (see the Gemma note on {@link MODEL_ID_TO_ICON}).
 *   Most vendors are in this bucket: OpenAI publishes exactly one mark for the
 *   entire GPT fleet, and Meta one for all of Llama.
 *
 * `anthropic` stays even though `claude` now supersedes it for every Claude
 * model: removing a slug would orphan any record an admin already saved with it
 * (the backend rejects an unknown slug on the next write), and it remains the
 * right answer for a non-Claude Anthropic model.
 */
export const BUILTIN_MODEL_ICONS = [
  'claude',
  'kimi',
  'qwen',
  'anthropic',
  'openai',
  'amazon',
  'meta',
  'google',
] as const;

export type BuiltinModelIcon = (typeof BUILTIN_MODEL_ICONS)[number];

/** Display names for the admin form's icon picker. */
export const BUILTIN_MODEL_ICON_LABELS: Record<BuiltinModelIcon, string> = {
  claude: 'Claude',
  kimi: 'Kimi',
  qwen: 'Qwen',
  anthropic: 'Anthropic',
  openai: 'OpenAI',
  amazon: 'Amazon',
  meta: 'Meta',
  google: 'Google',
};

/**
 * Model-family marks, matched against `modelId`.
 *
 * Checked *before* {@link PROVIDER_NAME_TO_ICON}: when a vendor brands its model
 * family separately from itself, the family mark is the more specific and more
 * recognisable answer. Anthropic is the case that motivated this — every model
 * we serve from them is a Claude, and the Claude starburst is what a user knows.
 *
 * Matched on `modelId` rather than `modelName` because the id is structured and
 * ours to rely on (`us.anthropic.claude-sonnet-4-6`), while the name is free text
 * an admin can rewrite to anything. The leading CRIS prefix (`us.`, `global.`)
 * varies per environment, so patterns anchor on a `.` boundary or the start.
 *
 * Order matters: first match wins.
 */
const MODEL_ID_TO_ICON: ReadonlyArray<readonly [RegExp, BuiltinModelIcon]> = [
  [/(?:^|\.)claude[-.]/, 'claude'],
  [/(?:^|\.)kimi[-.]/, 'kimi'],
  [/(?:^|\.)qwen/, 'qwen'],
  // Gemma is the deliberate exception to "model mark beats company mark": the
  // family HAS its own logo, and we still serve Google's. Gemma's mark is a gem
  // drawn with its construction scaffolding — a circle and crosshair guides
  // 0.186 units wide in a 24-unit box, i.e. 0.2px at the 28px tile these render
  // at. It smears into an indistinct blob, and the official colour variant fades
  // to #B1C5FF, which all but vanishes on the light tile. Stripping the
  // scaffolding to the bare gem would fix legibility but invents a mark Google
  // does not publish — and lands a near-twin of the Gemini spark. So the rule
  // yields to the thing the rule is FOR: a mark the user can actually identify.
  // Recorded here rather than left to the provider-name guess so a Gemma row
  // whose providerName reads "Google DeepMind" still resolves.
  [/(?:^|\.)gemma[-.]/, 'google'],
];

export function iconForModelId(modelId: string | null | undefined): BuiltinModelIcon | null {
  if (!modelId) return null;
  const id = modelId.trim().toLowerCase();
  if (!id) return null;
  return MODEL_ID_TO_ICON.find(([pattern]) => pattern.test(id))?.[1] ?? null;
}

/**
 * `providerName` values that name a company we ship a logo for.
 *
 * The last-resort fallback, and deliberately last: it is a guess from a free-text
 * field an admin typed. A model whose `providerName` is "Anthropic (via Bedrock)"
 * gets nothing from this and needs an explicit `iconSlug` — which is exactly why
 * the slug exists rather than this map being the whole feature.
 *
 * Vendors whose family mark is keyed off the model id above still appear here, as
 * the answer for a record whose `modelId` we do not recognise.
 */
const PROVIDER_NAME_TO_ICON: Record<string, BuiltinModelIcon> = {
  anthropic: 'anthropic',
  claude: 'claude',
  openai: 'openai',
  amazon: 'amazon',
  aws: 'amazon',
  meta: 'meta',
  // Moonshot AI brands its models "Kimi" the way OpenAI brands Codex — a product
  // mark that is also the model family's name. 'Moonshot AI' is what the curated
  // Kimi K3 row carries in `providerName`.
  'moonshot ai': 'kimi',
  moonshot: 'kimi',
  kimi: 'kimi',
  // Qwen is Alibaba's model family; the curated row names the family, not the
  // company, so this is the match that fires for it.
  qwen: 'qwen',
  alibaba: 'qwen',
  google: 'google',
  'google deepmind': 'google',
  gemma: 'google',
};

export function iconForProviderName(providerName: string | null | undefined): BuiltinModelIcon | null {
  if (!providerName) return null;
  return PROVIDER_NAME_TO_ICON[providerName.trim().toLowerCase()] ?? null;
}

function isBuiltinIcon(slug: string | null | undefined): slug is BuiltinModelIcon {
  return !!slug && (BUILTIN_MODEL_ICONS as readonly string[]).includes(slug);
}

/** Path to one half of a built-in logo's light/dark pair. */
export function builtinIconPath(slug: BuiltinModelIcon, theme: 'light' | 'dark'): string {
  return `/img/provider-logos/${slug}/${theme}.svg`;
}

/** Which rule produced a built-in logo — drives the admin form's caption. */
export type BuiltinIconVia = 'slug' | 'model' | 'provider';

/**
 * What to draw for a model, in precedence order.
 *
 * `upload` first: an admin who uploaded a file after picking a built-in logo meant
 * the file. Then the explicit `iconSlug`. Then the two guesses, **model family
 * before company** — a shipped SVG stays crisp and theme-correct at any size,
 * which a stored raster cannot. `none` is a real outcome, not a failure: the
 * picker falls back to a monogram rather than an empty gap.
 */
export type ModelIconSource =
  | { kind: 'upload'; url: string }
  | { kind: 'builtin'; slug: BuiltinModelIcon; via: BuiltinIconVia }
  | { kind: 'none' };

/** The subset of a model the icon is derived from. */
export type ModelIconInput = Pick<ManagedModel, 'iconUrl' | 'iconSlug' | 'providerName'> & {
  modelId?: string | null;
};

export function resolveModelIcon(model: ModelIconInput | null | undefined): ModelIconSource {
  if (!model) return { kind: 'none' };
  if (model.iconUrl) return { kind: 'upload', url: model.iconUrl };
  if (isBuiltinIcon(model.iconSlug)) return { kind: 'builtin', slug: model.iconSlug, via: 'slug' };

  const fromModel = iconForModelId(model.modelId);
  if (fromModel) return { kind: 'builtin', slug: fromModel, via: 'model' };

  const fromProvider = iconForProviderName(model.providerName);
  return fromProvider ? { kind: 'builtin', slug: fromProvider, via: 'provider' } : { kind: 'none' };
}
