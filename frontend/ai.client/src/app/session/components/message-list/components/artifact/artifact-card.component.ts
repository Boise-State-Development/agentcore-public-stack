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
  heroArrowLongRight,
} from '@ng-icons/heroicons/outline';
import type { Artifact } from '../../../../services/artifacts/artifact.model';
import { ArtifactStateService } from '../../../../services/artifacts/artifact-state.service';

/** Visual treatment derived from an artifact's content type. */
interface ArtifactKind {
  /** Short type label (HTML, JS, MD…). */
  label: string;
  /** Heroicon name for the type stamp. */
  icon: string;
  /** Single accent color (CSS). Used sparingly — a 2px edge rule, the
   *  stamp outline, and the type glyph — never as a fill. One mid tone
   *  that holds up on both the light and dark surface. */
  accent: string;
}

/**
 * One artifact, presented as a calm, rounded card — deliberately
 * un-"component-kit": a borderless tinted surface (radius matched to the
 * chat input), a hairline type stamp, and a quiet sans metadata line.
 * The lone accent color appears only as a thin left rule, the stamp
 * outline, and the glyph — not as a filled tile or pill.
 *
 * The artifact's content is never inlined here — opening asks the panel
 * to mint a short-lived render token and load it in a sandboxed iframe.
 *
 * Anchored inline after its producing assistant message by the
 * message-list (via `Artifact.producedByMessageIndex`); only legacy /
 * unanchorable artifacts fall back to the end-of-conversation strip.
 *
 * Accent is applied via `[style.color]` (not a bound class string) so
 * the structural classes stay static and there's no class-merge
 * ambiguity; `currentColor` then carries it into the rule/stamp/glyph.
 */
@Component({
  selector: 'app-artifact-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroCodeBracket,
      heroDocumentText,
      heroArrowLongRight,
    }),
  ],
  template: `
    <button
      type="button"
      class="artifact-card"
      [attr.aria-label]="ariaLabel()"
      (click)="open()"
    >
      <span class="artifact-card__surface">
        <span
          class="artifact-card__rule"
          [style.color]="kind().accent"
          aria-hidden="true"
        ></span>

        <span
          class="artifact-card__stamp"
          [style.color]="kind().accent"
          aria-hidden="true"
        >
          <ng-icon [name]="kind().icon" />
        </span>

        <span class="artifact-card__body">
          <span class="artifact-card__title">{{
            artifact().title || 'Untitled artifact'
          }}</span>
          <span class="artifact-card__meta">
            <span class="artifact-card__type">{{ kind().label }}</span>
            <span class="artifact-card__sep" aria-hidden="true">·</span>
            <span>v{{ artifact().version }}</span>
            @if (updatedLabel()) {
              <span class="artifact-card__sep" aria-hidden="true">·</span>
              <span>{{ updatedLabel() }}</span>
            }
          </span>
        </span>

        <span class="artifact-card__open" aria-hidden="true">
          <ng-icon name="heroArrowLongRight" />
        </span>
      </span>
    </button>
  `,
  styles: `
    :host {
      display: block;
    }

    /* The focusable element carries no visual chrome; it owns only the
       focus ring (an un-clipped rectangle, so the notched corner can't
       eat it). */
    .artifact-card {
      appearance: none;
      -webkit-appearance: none;
      margin: 0;
      padding: 0;
      border: 0;
      background: none;
      color: inherit;
      font: inherit;
      text-align: left;
      display: block;
      width: 100%;
      max-width: 28rem;
      /* matches the chat input's rounded-2xl so the focus ring (and the
         surface below) share the app's corner radius */
      border-radius: 1rem;
      cursor: pointer;
    }

    .artifact-card:focus-visible {
      outline: 2px solid #2563eb;
      outline-offset: 3px;
    }

    :host-context(html.dark) .artifact-card:focus-visible {
      outline-color: #60a5fa;
    }

    /* The visible body: a borderless, tinted, generously rounded card.
       overflow:hidden so the left rule conforms to the rounded edge. */
    .artifact-card__surface {
      position: relative;
      display: grid;
      grid-template-columns: auto minmax(0, 1fr) auto;
      align-items: center;
      gap: 0.875rem;
      padding: 0.8rem 1rem 0.8rem 1.1rem;
      background: #f1f2f4;
      border-radius: 1rem;
      overflow: hidden;
      transition: background-color 0.18s ease;
    }

    :host-context(html.dark) .artifact-card__surface {
      background: rgba(255, 255, 255, 0.045);
    }

    .artifact-card:hover .artifact-card__surface {
      background: #e7e8ec;
    }

    :host-context(html.dark) .artifact-card:hover .artifact-card__surface {
      background: rgba(255, 255, 255, 0.08);
    }

    /* Thin left rule: a short tick at rest, runs the full height on
       hover/focus. The card's only structural line. */
    .artifact-card__rule {
      position: absolute;
      left: 0;
      top: 50%;
      width: 2px;
      height: 1.15rem;
      transform: translateY(-50%);
      background: currentColor;
      opacity: 0.65;
      transition:
        height 0.2s ease,
        opacity 0.2s ease;
    }

    .artifact-card:hover .artifact-card__rule,
    .artifact-card:focus-visible .artifact-card__rule {
      height: 100%;
      opacity: 1;
    }

    /* Hairline type stamp — outline only, no fill. */
    .artifact-card__stamp {
      display: grid;
      place-items: center;
      width: 2rem;
      height: 2rem;
      border: 1px solid color-mix(in srgb, currentColor 32%, transparent);
      border-radius: 4px;
    }

    .artifact-card__stamp ng-icon {
      font-size: 1rem;
      line-height: 1;
    }

    .artifact-card__body {
      min-width: 0;
    }

    .artifact-card__title {
      display: block;
      font-size: 0.875rem;
      font-weight: 600;
      letter-spacing: -0.006em;
      color: #1f2430;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    :host-context(html.dark) .artifact-card__title {
      color: #eceef2;
    }

    /* Metadata line — the app's sans, small and quiet. */
    .artifact-card__meta {
      display: flex;
      align-items: center;
      gap: 0.4rem;
      margin-top: 0.2rem;
      font-size: 0.75rem;
      color: #4b5563;
    }

    :host-context(html.dark) .artifact-card__meta {
      color: #9aa3b2;
    }

    .artifact-card__type {
      text-transform: uppercase;
      font-weight: 600;
      letter-spacing: 0.07em;
      color: #353c4a;
    }

    :host-context(html.dark) .artifact-card__type {
      color: #c4cbd8;
    }

    .artifact-card__sep {
      opacity: 0.4;
    }

    /* Quiet open affordance: a long thin arrow that settles in on
       hover. No label, no shouting. */
    .artifact-card__open {
      display: flex;
      align-items: center;
      color: #9ca3af;
      opacity: 0.5;
      transform: translateX(-3px);
      transition:
        opacity 0.18s ease,
        transform 0.18s ease,
        color 0.18s ease;
    }

    .artifact-card__open ng-icon {
      font-size: 1.05rem;
      line-height: 1;
    }

    .artifact-card:hover .artifact-card__open,
    .artifact-card:focus-visible .artifact-card__open {
      opacity: 1;
      transform: translateX(0);
      color: #4b5563;
    }

    :host-context(html.dark) .artifact-card__open {
      color: #6b7280;
    }

    :host-context(html.dark) .artifact-card:hover .artifact-card__open,
    :host-context(html.dark) .artifact-card:focus-visible .artifact-card__open {
      color: #aab2c0;
    }

    @media (prefers-reduced-motion: reduce) {
      .artifact-card__surface,
      .artifact-card__rule,
      .artifact-card__open {
        transition: none;
      }
      .artifact-card__open {
        transform: none;
      }
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

const CODE = 'heroCodeBracket';
const DOC = 'heroDocumentText';

/** Map a MIME type to a label + accent. The match is on the bare type
 *  (parameters like `; charset=utf-8` are ignored). One mid-tone accent
 *  per type — legible on both the light and dark surface, used only as
 *  a thin rule / outline / glyph. */
function classifyContentType(contentType: string): ArtifactKind {
  const mime = (contentType || '').split(';')[0].trim().toLowerCase();

  switch (mime) {
    case 'text/html':
    case 'application/xhtml+xml':
      return { label: 'HTML', icon: CODE, accent: '#e8762a' };
    case 'text/javascript':
    case 'application/javascript':
      return { label: 'JS', icon: CODE, accent: '#cf9a13' };
    case 'text/css':
      return { label: 'CSS', icon: CODE, accent: '#2f9bd6' };
    case 'application/json':
      return { label: 'JSON', icon: CODE, accent: '#1f9d6b' };
    case 'image/svg+xml':
      return { label: 'SVG', icon: CODE, accent: '#d6519a' };
    case 'text/markdown':
      return { label: 'MD', icon: DOC, accent: '#7c6cf0' };
    default:
      return {
        label: mime.startsWith('text/') ? 'TEXT' : 'DOC',
        icon: DOC,
        accent: '#8a93a3',
      };
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
