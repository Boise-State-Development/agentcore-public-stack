import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroAcademicCap,
  heroChatBubbleLeftRight,
  heroCloud,
  heroCodeBracket,
  heroLink,
  heroVideoCamera,
} from '@ng-icons/heroicons/outline';
import { ConnectorType } from '../models/connector.model';
import { getBrandIconDataUrl } from '../models/connector-brand-icons';

export type ConnectorLogoSize = 'sm' | 'md' | 'lg';

const TILE_SIZE: Record<ConnectorLogoSize, string> = {
  sm: 'size-8 rounded-lg',
  md: 'size-10 rounded-xl',
  lg: 'size-14 rounded-2xl',
};

const IMAGE_SIZE: Record<ConnectorLogoSize, string> = {
  sm: 'size-5',
  md: 'size-6',
  lg: 'size-9',
};

const GLYPH_SIZE: Record<ConnectorLogoSize, string> = {
  sm: 'size-4',
  md: 'size-5',
  lg: 'size-6',
};

/**
 * A connector's mark in a tile: its uploaded icon if it has one, else the
 * vendor's brand logo, else a generic glyph.
 *
 * Logos sit on a white tile in both themes because a brand mark is a fixed
 * colour (X, GitHub and Notion are black) and cannot follow the dark surface.
 * The glyph fallback is the neutral icon tile from the app conventions, with
 * the colour on the wrapper because utilities set on `<ng-icon>` itself are
 * not reliably applied.
 */
@Component({
  selector: 'app-connector-logo',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroAcademicCap,
      heroChatBubbleLeftRight,
      heroCloud,
      heroCodeBracket,
      heroLink,
      heroVideoCamera,
    }),
  ],
  host: { class: 'inline-flex shrink-0' },
  template: `
    <span
      class="flex items-center justify-center"
      [class]="tileClass()"
      aria-hidden="true"
    >
      @if (src(); as source) {
        <img [src]="source" alt="" [class]="imageSize()" class="object-contain" />
      } @else {
        <ng-icon [name]="glyph()" [class]="glyphSize()" />
      }
    </span>
  `,
})
export class ConnectorLogoComponent {
  /** The connector's provider type; selects the brand logo. */
  readonly type = input<ConnectorType | null>(null);
  /** An uploaded icon (data URL). Wins over the brand logo. */
  readonly iconData = input<string | null | undefined>(null);
  /** Generic glyph used when there is no image at all. */
  readonly iconName = input<string | null | undefined>('heroLink');
  readonly size = input<ConnectorLogoSize>('md');

  protected readonly src = computed(() => {
    const uploaded = this.iconData();
    if (uploaded) return uploaded;
    const type = this.type();
    return type ? getBrandIconDataUrl(type) : null;
  });

  protected readonly glyph = computed(() => this.iconName() || 'heroLink');

  protected readonly tileClass = computed(() => {
    const size = TILE_SIZE[this.size()];
    return this.src()
      ? `${size} bg-white ring-1 ring-inset ring-gray-200 dark:ring-gray-600`
      : `${size} bg-gray-100 text-primary-accessible dark:bg-gray-700 dark:text-primary-50`;
  });

  protected readonly imageSize = computed(() => IMAGE_SIZE[this.size()]);
  protected readonly glyphSize = computed(() => GLYPH_SIZE[this.size()]);
}
