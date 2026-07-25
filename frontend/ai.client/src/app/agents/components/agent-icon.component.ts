import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
} from '@angular/core';
import { ConfigService } from '../../services/config.service';

/** The four sizes the store renders an Agent at (D5). */
export type AgentIconSize = 28 | 40 | 52 | 84;

/**
 * Per-size box, corner radius and glyph scale.
 *
 * Two ratios are held constant across the four sizes, and both had to be tuned rather
 * than inherited from the type scale:
 *
 * * **Radius ~29% of the box.** A constant `rounded-2xl` reads as a circle at 28px and
 *   as a barely-softened square at 84px, so the curve steps with the box.
 * * **Glyph ~50% of the box.** Stepping the Tailwind text scale (`text-xl` → `text-4xl`)
 *   grows the glyph more slowly than the tile, so the emoji drifts from 54% of a 28px
 *   tile down to 43% of an 84px one — the same icon looking progressively emptier the
 *   larger it gets.
 */
const SIZE_CLASSES: Record<AgentIconSize, string> = {
  28: 'size-7 rounded-lg text-[15px]',
  40: 'size-10 rounded-xl text-xl',
  52: 'size-13 rounded-2xl text-[26px]',
  84: 'size-21 rounded-[1.5rem] text-[42px]',
};

/**
 * The generated fallback's palette (D5).
 *
 * A curated set of pairs rather than hue math on the hash. Free-running hue arithmetic
 * reliably produces a handful of muddy or low-contrast tiles, and this is the *designed*
 * default for most of the store — not a placeholder — so every draw has to look
 * deliberate. All twelve are mid-to-dark and saturated, which keeps a white-ish emoji
 * legible on them in both themes.
 */
const GRADIENTS: readonly (readonly [string, string])[] = [
  ['#1f6feb', '#6f42c1'],
  ['#0ea5e9', '#2563eb'],
  ['#14b8a6', '#0e7490'],
  ['#22c55e', '#15803d'],
  ['#f59e0b', '#ea580c'],
  ['#ef4444', '#b91c1c'],
  ['#ec4899', '#be185d'],
  ['#8b5cf6', '#4c1d95'],
  ['#06b6d4', '#3b82f6'],
  ['#84cc16', '#16a34a'],
  ['#f97316', '#db2777'],
  ['#64748b', '#334155'],
];

/**
 * FNV-1a, 32-bit. Any stable hash would do; what matters is that it is computed the same
 * way on every surface, so an Agent's tile is the same tile in the store, in My Agents
 * and in the chat header. A per-surface hash would make the same Agent look like three.
 */
export function hashAgentId(agentId: string): number {
  let hash = 0x811c9dc5;
  for (let i = 0; i < agentId.length; i++) {
    hash ^= agentId.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return hash >>> 0;
}

/** The gradient an Agent id deterministically draws. */
export function gradientFor(agentId: string): string {
  const [from, to] = GRADIENTS[hashAgentId(agentId) % GRADIENTS.length];
  return `linear-gradient(135deg, ${from}, ${to})`;
}

/**
 * An Agent's square identity, at one of the four store sizes (D5).
 *
 * Renders the uploaded icon when there is one and the **generated gradient** when there
 * is not. The gradient is the designed default, not a placeholder to be replaced later:
 * an app store where a third of the tiles are a bare emoji on grey reads as unfinished,
 * and most Agents will never have an uploaded icon.
 *
 * `iconUrl` arrives from the API as a relative path (`/agents/{id}/icon?v=…`) so the
 * container never has to know its own public origin; the API base is prefixed here.
 * The `?v=` is the icon's content digest, which is what lets the response be cached
 * `immutable` and still change the instant a new icon is uploaded.
 *
 * A load failure falls back to the gradient rather than showing a broken tile — the
 * backend answers 404 for a key that outlived its object, and that path has to land
 * somewhere composed.
 */
@Component({
  selector: 'app-agent-icon',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (src() && !failed()) {
      <img
        [src]="src()"
        [alt]="alt()"
        [class]="boxClasses()"
        class="shrink-0 object-cover ring-1 ring-black/5 dark:ring-white/10"
        loading="lazy"
        decoding="async"
        (error)="failed.set(true)"
      />
    } @else {
      <span
        [class]="boxClasses()"
        [style.background-image]="gradient()"
        class="flex shrink-0 items-center justify-center ring-1 ring-black/5 select-none dark:ring-white/10"
        [attr.role]="alt() ? 'img' : null"
        [attr.aria-label]="alt() || null"
        [attr.aria-hidden]="alt() ? null : true"
      >
        <span class="leading-none drop-shadow-sm">{{ emoji() || '✦' }}</span>
      </span>
    }
  `,
})
export class AgentIconComponent {
  private config = inject(ConfigService);

  readonly agentId = input.required<string>();
  /** Relative API path from the read shape; absent → the generated gradient. */
  readonly iconUrl = input<string | undefined>();
  readonly emoji = input<string | undefined>();
  readonly size = input<AgentIconSize>(40);
  /** Empty (the default) marks the tile decorative, for rows that already name the agent. */
  readonly alt = input<string>('');

  readonly src = computed(() => {
    const url = this.iconUrl();
    if (!url) return null;
    // Only a leading `/` marks an API path needing the base. Anything else is already
    // absolute — including the `blob:` URL the upload dialog previews a local file with.
    return url.startsWith('/') ? `${this.config.appApiUrl()}${url}` : url;
  });

  /** Reset on every new src, so replacing a broken icon re-attempts the load. */
  readonly failed = linkedSignal<string | null, boolean>({
    source: this.src,
    computation: () => false,
  });

  readonly boxClasses = computed(() => SIZE_CLASSES[this.size()]);
  readonly gradient = computed(() => gradientFor(this.agentId()));
}
