import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroCodeBracket,
  heroDocumentText,
  heroArrowTopRightOnSquare,
} from '@ng-icons/heroicons/outline';
import type { Artifact } from '../../../../services/artifacts/artifact.model';
import { ArtifactStateService } from '../../../../services/artifacts/artifact-state.service';

/** Visual treatment derived from an artifact's content type. */
interface ArtifactKind {
  label: string;
  icon: string;
  /** Full class list for the icon tile (no static classes — see note). */
  tile: string;
  /** Full class list for the type badge. */
  badge: string;
  /** Full class list for the left accent rail. */
  rail: string;
}

/**
 * Compact, clickable card for one artifact. The artifact's content is
 * never inlined here — opening the card asks the panel to mint a
 * short-lived render token and load it in a sandboxed iframe.
 *
 * Anchored inline after its producing assistant message by the
 * message-list (via `Artifact.producedByMessageIndex`); only legacy /
 * unanchorable artifacts fall back to the end-of-conversation strip.
 *
 * The colored sub-elements bind `[class]` to a full computed class
 * string (no static `class` on those nodes) so the accent swaps cleanly
 * by content type without static/dynamic class-merge ambiguity.
 */
@Component({
  selector: 'app-artifact-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroCodeBracket,
      heroDocumentText,
      heroArrowTopRightOnSquare,
    }),
  ],
  template: `
    <button
      type="button"
      class="group relative flex w-full max-w-md items-center gap-3 overflow-hidden rounded-xl border border-gray-200/80 bg-white py-3 pl-4 pr-3.5 text-left shadow-sm transition-all duration-150 hover:-translate-y-px hover:border-gray-300 hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2 dark:border-gray-700/80 dark:bg-gray-800/70 dark:hover:border-gray-600 dark:focus-visible:ring-offset-gray-900"
      [attr.aria-label]="ariaLabel()"
      (click)="open()"
    >
      <span aria-hidden="true" [class]="kind().rail"></span>

      <span [class]="kind().tile">
        <ng-icon [name]="kind().icon" class="text-lg" aria-hidden="true" />
      </span>

      <span class="min-w-0 flex-1">
        <span
          class="block truncate text-sm font-semibold text-gray-900 dark:text-gray-100"
        >
          {{ artifact().title || 'Untitled artifact' }}
        </span>
        <span
          class="mt-1 flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400"
        >
          <span [class]="kind().badge">{{ kind().label }}</span>
          <span>v{{ artifact().version }}</span>
          @if (updatedLabel()) {
            <span aria-hidden="true" class="text-gray-300 dark:text-gray-600"
              >·</span
            >
            <span>{{ updatedLabel() }}</span>
          }
        </span>
      </span>

      <span
        class="flex shrink-0 items-center gap-1 text-xs font-medium text-gray-400 transition-colors group-hover:text-blue-600 dark:text-gray-500 dark:group-hover:text-blue-300"
      >
        <span class="hidden sm:inline">Open</span>
        <ng-icon
          name="heroArrowTopRightOnSquare"
          class="text-base"
          aria-hidden="true"
        />
      </span>
    </button>
  `,
  styles: `
    :host {
      display: block;
    }
  `,
})
export class ArtifactCardComponent {
  artifact = input.required<Artifact>();

  private artifactState = inject(ArtifactStateService);

  protected readonly kind = computed<ArtifactKind>(() =>
    classifyContentType(this.artifact().contentType),
  );

  /** Short, human relative time for the meta line. Empty when the
   *  timestamp is missing or unparseable so the row just omits it. */
  protected readonly updatedLabel = computed<string>(() =>
    relativeTime(this.artifact().updatedAt),
  );

  protected readonly ariaLabel = computed(
    () =>
      `Open ${this.kind().label} artifact ${this.artifact().title || 'Untitled'}, version ${this.artifact().version}`,
  );

  protected open(): void {
    const a = this.artifact();
    this.artifactState.openArtifactPanel({
      artifactId: a.artifactId,
      version: a.version,
      title: a.title,
    });
  }
}

const TILE_BASE =
  'flex h-10 w-10 shrink-0 items-center justify-center rounded-lg';
const BADGE_BASE =
  'inline-flex items-center rounded-full px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide';
const RAIL_BASE = 'absolute inset-y-0 left-0 w-1';

function kind(
  label: string,
  icon: string,
  tile: string,
  badge: string,
  rail: string,
): ArtifactKind {
  return {
    label,
    icon,
    tile: `${TILE_BASE} ${tile}`,
    badge: `${BADGE_BASE} ${badge}`,
    rail: `${RAIL_BASE} ${rail}`,
  };
}

const CODE = 'heroCodeBracket';
const DOC = 'heroDocumentText';

/** Map a MIME type to a label + accent palette. The match is on the
 *  bare type (parameters like `; charset=utf-8` are ignored). */
function classifyContentType(contentType: string): ArtifactKind {
  const mime = (contentType || '').split(';')[0].trim().toLowerCase();

  switch (mime) {
    case 'text/html':
    case 'application/xhtml+xml':
      return kind(
        'HTML',
        CODE,
        'bg-orange-100 text-orange-600 dark:bg-orange-900/40 dark:text-orange-300',
        'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300',
        'bg-orange-400 dark:bg-orange-500',
      );
    case 'text/javascript':
    case 'application/javascript':
      return kind(
        'JS',
        CODE,
        'bg-amber-100 text-amber-600 dark:bg-amber-900/40 dark:text-amber-300',
        'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
        'bg-amber-400 dark:bg-amber-500',
      );
    case 'text/css':
      return kind(
        'CSS',
        CODE,
        'bg-sky-100 text-sky-600 dark:bg-sky-900/40 dark:text-sky-300',
        'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300',
        'bg-sky-400 dark:bg-sky-500',
      );
    case 'application/json':
      return kind(
        'JSON',
        CODE,
        'bg-emerald-100 text-emerald-600 dark:bg-emerald-900/40 dark:text-emerald-300',
        'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
        'bg-emerald-400 dark:bg-emerald-500',
      );
    case 'image/svg+xml':
      return kind(
        'SVG',
        CODE,
        'bg-pink-100 text-pink-600 dark:bg-pink-900/40 dark:text-pink-300',
        'bg-pink-100 text-pink-700 dark:bg-pink-900/40 dark:text-pink-300',
        'bg-pink-400 dark:bg-pink-500',
      );
    case 'text/markdown':
      return kind(
        'MD',
        DOC,
        'bg-violet-100 text-violet-600 dark:bg-violet-900/40 dark:text-violet-300',
        'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
        'bg-violet-400 dark:bg-violet-500',
      );
    default:
      return kind(
        mime.startsWith('text/') ? 'TEXT' : 'DOC',
        DOC,
        'bg-slate-100 text-slate-600 dark:bg-slate-700/60 dark:text-slate-300',
        'bg-slate-100 text-slate-600 dark:bg-slate-700/60 dark:text-slate-300',
        'bg-slate-400 dark:bg-slate-500',
      );
  }
}

/** "just now" / "5m ago" / "3h ago" / "2d ago", else a short date.
 *  Returns '' for a missing or unparseable timestamp. */
function relativeTime(iso: string): string {
  if (!iso) return '';
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return '';

  const diffMs = Date.now() - then;
  const min = Math.floor(diffMs / 60000);
  if (min < 1) return 'just now';
  if (min < 60) return `${min}m ago`;

  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;

  const day = Math.floor(hr / 24);
  if (day < 7) return `${day}d ago`;

  return new Date(then).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
  });
}
