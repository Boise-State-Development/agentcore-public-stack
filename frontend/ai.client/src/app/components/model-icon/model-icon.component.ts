import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
} from '@angular/core';
import { ConfigService } from '../../services/config.service';
import { ManagedModel } from '../../admin/manage-models/models/managed-model.model';
import { builtinIconPath, resolveModelIcon } from '../../admin/manage-models/models/model-icons';

/** The sizes a model avatar renders at: menu row, form row, catalog card. */
export type ModelIconSize = 20 | 24 | 28 | 40;

/**
 * Per-size box, corner radius and monogram type scale.
 *
 * The glyph scale is tuned per box rather than stepped off the Tailwind text
 * scale: a single `text-xs` reads as cramped at 40px and as oversized at 20px,
 * which is the same drift the Agent tile documents.
 */
const SIZE_CLASSES: Record<ModelIconSize, string> = {
  20: 'size-5 rounded-md text-[9px]',
  24: 'size-6 rounded-md text-[10px]',
  28: 'size-7 rounded-lg text-[11px]',
  40: 'size-10 rounded-xl text-sm',
};

/** How much of the box the logo itself fills — the rest is breathing room. */
const GLYPH_CLASSES: Record<ModelIconSize, string> = {
  20: 'size-3.5',
  24: 'size-4',
  28: 'size-5',
  40: 'size-7',
};

/**
 * A managed model's avatar: the uploaded icon, the built-in vendor logo, or a
 * monogram.
 *
 * The three cases are not three fallbacks of decreasing quality — see
 * `resolveModelIcon`. A built-in slug is the *preferred* answer for a vendor we
 * ship a logo for, because it stays a crisp, theme-correct vector at every size,
 * and it costs no storage and no round trip. The upload exists for the models
 * that map to no vendor we ship: an in-house fine-tune, a new provider.
 *
 * Built-in logos come in a light/dark pair, and which one is correct is a CSS
 * question, not a TypeScript one — the pair is rendered with `dark:hidden` /
 * `hidden dark:block` (matching the admin catalog cards) so switching theme never
 * waits on a signal or a re-render.
 *
 * `iconUrl` arrives from the API as a relative path (`/models/{id}/icon?v=…`) so
 * the container never has to know its own public origin; the API base is prefixed
 * here. The `?v=` is the icon's content digest, which is what lets the response be
 * cached `immutable` and still change the instant a new icon is uploaded.
 *
 * A load failure falls through to the monogram rather than leaving a broken tile:
 * the backend answers 404 for a key that outlived its object, and a menu row is a
 * place that has to stay composed.
 */
@Component({
  selector: 'app-model-icon',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'contents' },
  template: `
    @if (source(); as icon) {
      @if (icon.kind === 'upload' && !failed()) {
        <img
          [src]="uploadSrc()"
          [alt]="alt()"
          [class]="boxClasses()"
          class="shrink-0 bg-white object-cover ring-1 ring-black/5 dark:bg-gray-900 dark:ring-white/10"
          loading="lazy"
          decoding="async"
          (error)="failed.set(true)"
        />
      } @else if (icon.kind === 'builtin') {
        <span
          [class]="boxClasses()"
          class="flex shrink-0 items-center justify-center bg-gray-50 dark:bg-gray-900"
          [attr.role]="alt() ? 'img' : null"
          [attr.aria-label]="alt() || null"
          [attr.aria-hidden]="alt() ? null : true"
        >
          <img [src]="lightLogo()" alt="" [class]="glyphClasses()" class="block object-contain dark:hidden" />
          <img [src]="darkLogo()" alt="" [class]="glyphClasses()" class="hidden object-contain dark:block" />
        </span>
      } @else {
        <span
          [class]="boxClasses()"
          class="flex shrink-0 items-center justify-center bg-gray-100 font-semibold text-gray-500 select-none dark:bg-gray-700 dark:text-gray-300"
          [attr.role]="alt() ? 'img' : null"
          [attr.aria-label]="alt() || null"
          [attr.aria-hidden]="alt() ? null : true"
        >
          <span class="leading-none">{{ monogram() }}</span>
        </span>
      }
    }
  `,
})
export class ModelIconComponent {
  private config = inject(ConfigService);

  /**
   * Only the fields the icon is derived from, so a caller holding a partial
   * model (the admin form's live preview, a curated template) can render one
   * without inventing the rest of a `ManagedModel`.
   */
  readonly model = input.required<
    Pick<ManagedModel, 'iconUrl' | 'iconSlug' | 'providerName'> & { modelName?: string }
  >();
  readonly size = input<ModelIconSize>(24);
  /** Empty (the default) marks the tile decorative, for rows that already name the model. */
  readonly alt = input<string>('');

  readonly source = computed(() => resolveModelIcon(this.model()));

  readonly uploadSrc = computed(() => {
    const icon = this.source();
    if (icon.kind !== 'upload') return null;
    // Only a leading `/` marks an API path needing the base. Anything else is
    // already absolute — including the `blob:` URL the admin form previews a
    // freshly-picked local file with.
    return icon.url.startsWith('/') ? `${this.config.appApiUrl()}${icon.url}` : icon.url;
  });

  /** Reset on every new src, so replacing a broken icon re-attempts the load. */
  readonly failed = linkedSignal<string | null, boolean>({
    source: this.uploadSrc,
    computation: () => false,
  });

  readonly lightLogo = computed(() => {
    const icon = this.source();
    return icon.kind === 'builtin' ? builtinIconPath(icon.slug, 'light') : null;
  });

  readonly darkLogo = computed(() => {
    const icon = this.source();
    return icon.kind === 'builtin' ? builtinIconPath(icon.slug, 'dark') : null;
  });

  /**
   * First letter of the model's name, or of its provider when the name is
   * missing. A letter beats a generic glyph here: a picker of eight models all
   * showing the same placeholder square tells the reader nothing.
   */
  readonly monogram = computed(() => {
    const model = this.model();
    const source = model.modelName?.trim() || model.providerName?.trim() || '';
    return source ? source.charAt(0).toUpperCase() : '•';
  });

  readonly boxClasses = computed(() => SIZE_CLASSES[this.size()]);
  readonly glyphClasses = computed(() => GLYPH_CLASSES[this.size()]);
}
