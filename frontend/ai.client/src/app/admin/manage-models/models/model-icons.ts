import { ManagedModel } from './managed-model.model';

/**
 * Built-in vendor logos shipped with the SPA.
 *
 * Each slug has a `public/img/provider-logos/{slug}/{light,dark}.svg` pair. Adding
 * a vendor means dropping the pair in, listing it here, and listing it in the
 * backend's `BUILTIN_MODEL_ICONS` (`apis/shared/models/model_icons.py`), which
 * validates the slug on write — a slug only one side knows is a tile that renders
 * as nothing, with no error anywhere to say why.
 */
export const BUILTIN_MODEL_ICONS = ['anthropic', 'openai', 'amazon', 'meta'] as const;

export type BuiltinModelIcon = (typeof BUILTIN_MODEL_ICONS)[number];

/** Display names for the admin form's icon picker. */
export const BUILTIN_MODEL_ICON_LABELS: Record<BuiltinModelIcon, string> = {
  anthropic: 'Anthropic',
  openai: 'OpenAI',
  amazon: 'Amazon',
  meta: 'Meta',
};

/**
 * `providerName` values that name a vendor we ship a logo for.
 *
 * The last-resort fallback, and deliberately last: it is a guess from a free-text
 * field an admin typed. A model whose `providerName` is "Anthropic (via Bedrock)"
 * gets nothing from this and needs an explicit `iconSlug` — which is exactly why
 * the slug exists rather than this map being the whole feature.
 */
const PROVIDER_NAME_TO_ICON: Record<string, BuiltinModelIcon> = {
  anthropic: 'anthropic',
  openai: 'openai',
  amazon: 'amazon',
  aws: 'amazon',
  meta: 'meta',
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

/**
 * What to draw for a model, in precedence order.
 *
 * `upload` first: an admin who uploaded a file after picking a built-in logo meant
 * the file. `builtin` next, from the explicit `iconSlug` and only then from the
 * provider-name guess — a shipped SVG stays crisp and theme-correct at any size,
 * which a stored raster cannot. `none` is a real outcome, not a failure: the picker
 * falls back to a monogram rather than an empty gap.
 */
export type ModelIconSource =
  | { kind: 'upload'; url: string }
  | { kind: 'builtin'; slug: BuiltinModelIcon }
  | { kind: 'none' };

export function resolveModelIcon(
  model: Pick<ManagedModel, 'iconUrl' | 'iconSlug' | 'providerName'> | null | undefined,
): ModelIconSource {
  if (!model) return { kind: 'none' };
  if (model.iconUrl) return { kind: 'upload', url: model.iconUrl };
  if (isBuiltinIcon(model.iconSlug)) return { kind: 'builtin', slug: model.iconSlug };
  const guessed = iconForProviderName(model.providerName);
  return guessed ? { kind: 'builtin', slug: guessed } : { kind: 'none' };
}
