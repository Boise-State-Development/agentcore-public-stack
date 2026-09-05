import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroCodeBracket,
  heroDocumentText,
  heroArrowDownTray,
  heroArrowPath,
  heroArrowUpOnSquare,
  heroPencilSquare,
  heroTrash,
} from '@ng-icons/heroicons/outline';
import type { Artifact } from '../../../../services/artifacts/artifact.model';
import { ArtifactStateService } from '../../../../services/artifacts/artifact-state.service';
import { ArtifactHttpService } from '../../../../services/artifacts/artifact-http.service';
import { ArtifactDownloadService } from '../../../../services/artifacts/artifact-download.service';
import {
  ArtifactShareModalComponent,
  type ArtifactShareModalData,
} from './artifact-share-modal.component';
import { UserService } from '../../../../../auth/user.service';
import { TooltipDirective } from '../../../../../components/tooltip/tooltip.directive';
import { ToastService } from '../../../../../services/toast/toast.service';
import {
  ConfirmationDialogComponent,
  type ConfirmationDialogData,
} from '../../../../../components/confirmation-dialog';
import {
  RenameArtifactDialogComponent,
  type RenameArtifactDialogData,
  type RenameArtifactDialogResult,
} from '../../../../../artifacts/components/rename-artifact-dialog.component';
import { parseIso } from '../../../../../utils/date';

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
  imports: [NgIcon, TooltipDirective],
  providers: [
    provideIcons({
      heroCodeBracket,
      heroDocumentText,
      heroArrowDownTray,
      heroArrowPath,
      heroArrowUpOnSquare,
      heroPencilSquare,
      heroTrash,
    }),
  ],
  template: `
    <div class="artifact-card">
      <button
        type="button"
        class="artifact-card__hit"
        [attr.aria-label]="ariaLabel()"
        (click)="open()"
      ></button>

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

        <span class="artifact-card__body" aria-hidden="true">
          <span class="artifact-card__title">{{
            artifact().title || 'Untitled artifact'
          }}</span>
          <span class="artifact-card__meta">
            <span class="artifact-card__type">{{ kind().label }}</span>
            <span class="artifact-card__sep">·</span>
            <span>v{{ artifact().version }}</span>
            @if (updatedLabel()) {
              <span class="artifact-card__sep">·</span>
              <span>{{ updatedLabel() }}</span>
            }
          </span>
        </span>

        <span class="artifact-card__actions">
          <button
            type="button"
            class="artifact-card__action"
            [attr.aria-label]="shareAriaLabel()"
            [appTooltip]="'Share version ' + artifact().version"
            (click)="share()"
          >
            <ng-icon name="heroArrowUpOnSquare" aria-hidden="true" />
            <span class="artifact-card__action-label">Share</span>
          </button>

          <button
            type="button"
            class="artifact-card__action"
            [class.is-busy]="downloading()"
            [attr.aria-label]="downloadAriaLabel()"
            [attr.aria-busy]="downloading()"
            [appTooltip]="'Download version ' + artifact().version"
            [disabled]="downloading()"
            (click)="download()"
          >
            <ng-icon
              [name]="downloading() ? 'heroArrowPath' : 'heroArrowDownTray'"
              aria-hidden="true"
            />
            <span class="artifact-card__action-label">Download</span>
          </button>

          <button
            type="button"
            class="artifact-card__action artifact-card__action--icon"
            [attr.aria-label]="renameAriaLabel()"
            [appTooltip]="'Rename artifact'"
            [disabled]="mutating()"
            (click)="rename()"
          >
            <ng-icon name="heroPencilSquare" aria-hidden="true" />
          </button>

          <button
            type="button"
            class="artifact-card__action artifact-card__action--icon artifact-card__action--danger"
            [attr.aria-label]="deleteAriaLabel()"
            [appTooltip]="'Delete artifact'"
            [disabled]="mutating()"
            (click)="confirmDelete()"
          >
            <ng-icon name="heroTrash" aria-hidden="true" />
          </button>
        </span>
      </span>
    </div>
  `,
  styles: `
    :host {
      display: block;
    }

    /* Card shell: a positioning context for the stretched open button
       and the download button. No chrome of its own. isolation:isolate
       keeps the internal z-index (hit=1, surface=2) scoped to the card
       so it can't paint over page overlays (e.g. the context popover) —
       the card as a whole stacks at its normal flow level. */
    .artifact-card {
      position: relative;
      isolation: isolate;
      display: block;
      width: 100%;
      /* Establishes the query container for the narrow-card rules at the
         bottom of this block. The card is sized by the chat column, not
         the viewport — the artifact panel docking open halves it — so a
         container query is correct here and a media query is not. */
      container-type: inline-size;
      /* matches the chat input's rounded-2xl so the focus ring and the
         surface share the app's corner radius */
      border-radius: 1rem;
    }

    /* Primary action: an invisible button stretched over the whole card.
       It owns the focus ring (an un-clipped rectangle so the rounded
       corner can't eat it). The surface above is pointer-events:none, so
       a click anywhere but the download button falls through to here. */
    .artifact-card__hit {
      position: absolute;
      inset: 0;
      z-index: 1;
      appearance: none;
      -webkit-appearance: none;
      margin: 0;
      padding: 0;
      border: 0;
      background: none;
      font: inherit;
      border-radius: inherit;
      cursor: pointer;
    }

    .artifact-card__hit:focus-visible {
      outline: 2px solid #2563eb;
      outline-offset: 3px;
    }

    :host-context(html.dark) .artifact-card__hit:focus-visible {
      outline-color: #60a5fa;
    }

    /* The visible body: a borderless, tinted, generously rounded card.
       overflow:hidden so the left rule conforms to the rounded edge.
       pointer-events:none delegates clicks to the stretched button
       beneath; the download button (col 3) re-enables them. The grid's
       auto last column sizes itself to the button — no manual gutter. */
    .artifact-card__surface {
      position: relative;
      z-index: 2;
      pointer-events: none;
      display: grid;
      grid-template-columns: auto minmax(0, 1fr) auto;
      align-items: center;
      gap: 0.875rem;
      padding: 0.8rem 1rem 0.8rem 1.1rem;
      background: var(--color-gray-100);
      border-radius: 1rem;
      overflow: hidden;
      transition: background-color 0.18s ease;
    }

    :host-context(html.dark) .artifact-card__surface {
      background: rgba(255, 255, 255, 0.045);
    }

    .artifact-card:hover .artifact-card__surface {
      background: var(--color-gray-200);
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
    .artifact-card__hit:focus-visible
      ~ .artifact-card__surface
      .artifact-card__rule {
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

    /* min-width:0 lets the 1fr column actually shrink; overflow:hidden
       is what stops its contents painting outside it. Without the
       latter the metadata line spills under the action buttons once the
       chat column narrows (e.g. with the artifact panel docked open). */
    .artifact-card__body {
      min-width: 0;
      overflow: hidden;
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
    /* Truncates as one line rather than wrapping or overflowing: the
       type/version/time chunks are progressively less important, so
       clipping from the right degrades in the right order. */
    .artifact-card__meta {
      display: flex;
      align-items: center;
      gap: 0.4rem;
      margin-top: 0.2rem;
      font-size: 0.75rem;
      color: #4b5563;
      min-width: 0;
      white-space: nowrap;
      overflow: hidden;
    }
    .artifact-card__meta > * {
      flex: 0 0 auto;
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

    /* Rename and Delete are icon-only at every width. They act on the
       whole artifact while Share and Download act on the version this
       card shows, and giving the whole-artifact pair the quieter
       treatment is what keeps that difference readable — four equally
       weighted labelled buttons would read as four peers. Their labels
       are visually hidden rather than absent, so the accessible name
       survives (WCAG 2.5.3), and [appTooltip] is what a sighted user
       gets in place of text. */
    .artifact-card__action--icon {
      padding-left: 0.4rem;
      padding-right: 0.4rem;
    }

    .artifact-card__action--danger:hover {
      color: #b42318;
      border-color: color-mix(in srgb, #b42318 45%, transparent);
      background: rgba(180, 35, 24, 0.07);
    }

    :host-context(html.dark) .artifact-card__action--danger:hover {
      color: #f9a8a0;
      border-color: color-mix(in srgb, #f9a8a0 45%, transparent);
      background: rgba(249, 168, 160, 0.12);
    }

    /* Secondary actions sit in the grid's last column.
       The row re-enables pointer events for itself so the buttons
       capture their own clicks while the rest of the card falls through
       to the stretched open button beneath. */
    .artifact-card__actions {
      pointer-events: auto;
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      /* The grid's last column is sized auto, so the actions always get
         the width they ask for; this just stops them being the thing
         that wraps. */
      flex: 0 0 auto;
    }

    /* A bordered, labelled action button. Both actions keep a visible
       text label, so no tooltip is needed for the accessible name
       (WCAG 2.5.3) — the icon-only variant would. Resting colour clears
       the 3:1 non-text contrast bar. */
    .artifact-card__action {
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      margin: 0;
      padding: 0.34rem 0.7rem;
      border: 1px solid color-mix(in srgb, currentColor 35%, transparent);
      border-radius: 7px;
      background: none;
      color: #6b7280;
      font: inherit;
      font-size: 0.75rem;
      font-weight: 600;
      line-height: 1;
      white-space: nowrap;
      cursor: pointer;
      transition:
        color 0.18s ease,
        border-color 0.18s ease,
        background-color 0.18s ease;
    }

    .artifact-card__action ng-icon {
      font-size: 0.95rem;
      line-height: 1;
    }

    .artifact-card:hover .artifact-card__action,
    .artifact-card__action:hover {
      color: #374151;
    }

    .artifact-card__action:hover {
      background: rgba(0, 0, 0, 0.05);
    }

    .artifact-card__action:focus-visible {
      outline: 2px solid #2563eb;
      outline-offset: 2px;
    }

    .artifact-card__action:disabled {
      cursor: default;
    }

    .artifact-card__action.is-busy ng-icon {
      animation: artifact-card-spin 0.8s linear infinite;
    }

    :host-context(html.dark) .artifact-card__action {
      color: #9aa3b2;
    }

    :host-context(html.dark) .artifact-card:hover .artifact-card__action,
    :host-context(html.dark) .artifact-card__action:hover {
      color: #cbd2dd;
    }

    :host-context(html.dark) .artifact-card__action:hover {
      background: rgba(255, 255, 255, 0.08);
    }

    :host-context(html.dark) .artifact-card__action:focus-visible {
      outline-color: #60a5fa;
    }

    @keyframes artifact-card-spin {
      to {
        transform: rotate(360deg);
      }
    }

    /* Narrow card (docked artifact panel, split view, mobile): the
       labelled buttons would otherwise consume the whole row and the
       title would clip to nothing. Drop the labels to icons and let the
       title win the space back. The threshold is 34rem rather than the
       original 26rem because the row now carries four controls, not
       two — Share and Download have to shed their text sooner to leave
       the title anything to occupy.

       The label is visually hidden rather than removed, so it stays in
       the accessible name (WCAG 2.5.3) — and the buttons carry
       [appTooltip], which is what a sighted user gets in place of the
       text they can no longer see. */
    @container (max-width: 34rem) {
      .artifact-card__action-label {
        position: absolute;
        width: 1px;
        height: 1px;
        padding: 0;
        margin: -1px;
        overflow: hidden;
        clip-path: inset(50%);
        white-space: nowrap;
      }
      .artifact-card__action {
        padding-left: 0.45rem;
        padding-right: 0.45rem;
      }
    }

    @media (prefers-reduced-motion: reduce) {
      .artifact-card__surface,
      .artifact-card__rule,
      .artifact-card__action {
        transition: none;
      }
      .artifact-card__action.is-busy ng-icon {
        animation: none;
      }
    }
  `,
})
export class ArtifactCardComponent {
  artifact = input.required<Artifact>();

  private artifactState = inject(ArtifactStateService);
  private artifactHttp = inject(ArtifactHttpService);
  private artifactDownload = inject(ArtifactDownloadService);
  private dialog = inject(Dialog);
  private userService = inject(UserService);
  private toast = inject(ToastService);

  protected readonly downloading = signal(false);
  /** A rename or delete is in flight, so both of those buttons wait. */
  protected readonly mutating = signal(false);

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

  /** Static so the visible "Download" label is always contained in the
   *  accessible name (WCAG 2.5.3); the working state rides `aria-busy`. */
  protected readonly downloadAriaLabel = computed(
    () =>
      `Download ${this.kind().label} artifact ${this.artifact().title || 'Untitled'}, version ${this.artifact().version}`,
  );

  protected readonly shareAriaLabel = computed(
    () =>
      `Share ${this.kind().label} artifact ${this.artifact().title || 'Untitled'}, version ${this.artifact().version}`,
  );

  /* Rename and Delete name no version, deliberately — and Delete says
     "and all versions" outright. This card is one of possibly several
     for the same artifact, each captioned with its own version number,
     so a label reading "Delete … version 2" next to the neighbouring
     "Download … version 2" would promise something these controls do
     not do: both act on the whole artifact. */
  protected readonly renameAriaLabel = computed(
    () => `Rename artifact ${this.artifact().title || 'Untitled'}`,
  );

  protected readonly deleteAriaLabel = computed(
    () =>
      `Delete artifact ${this.artifact().title || 'Untitled'} and all versions`,
  );

  protected open(): void {
    const a = this.artifact();
    this.artifactState.openArtifactPanel({
      artifactId: a.artifactId,
      version: a.version,
      title: a.title,
    });
  }

  /** Open the share dialog for *this* version.
   *
   *  The card shows one row per version, so the version it shares is the
   *  one the user is looking at — a share pins an immutable version and
   *  never follows HEAD. */
  protected share(): void {
    const a = this.artifact();
    this.dialog.open(ArtifactShareModalComponent, {
      data: {
        artifactId: a.artifactId,
        version: a.version,
        title: a.title,
        ownerEmail: this.userService.currentUser()?.email ?? '',
      } as ArtifactShareModalData,
    });
  }

  /** Retitle the whole artifact, not this version.
   *
   *  The backend writes the title to every version row, so the local
   *  registry follows — otherwise the sibling cards for the same
   *  artifact would keep the old name until a reload. */
  protected async rename(): Promise<void> {
    const a = this.artifact();
    const data: RenameArtifactDialogData = { title: a.title };
    const dialogRef = this.dialog.open<RenameArtifactDialogResult>(
      RenameArtifactDialogComponent,
      { data },
    );
    const title = await firstValueFrom(dialogRef.closed);
    if (!title) return;

    this.mutating.set(true);
    try {
      const updated = await this.artifactHttp.renameArtifact(
        a.artifactId,
        title,
      );
      this.artifactState.rename(a.artifactId, updated.title);
    } catch {
      this.toast.error(
        'Could not rename artifact',
        'The change was not saved. Try again in a moment.',
      );
    } finally {
      this.mutating.set(false);
    }
  }

  /** Delete the whole artifact after confirmation.
   *
   *  The confirmation copy leads with the version count, because this
   *  control sits on a card captioned "v2" and the neighbouring Share
   *  and Download buttons really are scoped to that version. The dialog
   *  is the last chance to correct that reading before the other cards
   *  vanish alongside this one.
   *
   *  Removal goes through the registry, which is what every sibling card
   *  and the docked panel read — one call clears them all. Only after
   *  the request succeeds: an optimistic removal would look like success
   *  and then reappear on the next session load. */
  protected async confirmDelete(): Promise<void> {
    const a = this.artifact();
    const versions = this.artifactState.versionsFor(a.artifactId).length;
    const scope =
      versions > 1
        ? `all ${versions} versions of "${a.title || 'Untitled artifact'}"`
        : `"${a.title || 'Untitled artifact'}"`;
    const data: ConfirmationDialogData = {
      title: 'Delete this artifact?',
      message:
        `This deletes ${scope} permanently, not just the version on this ` +
        'card, along with any share links you have created. This cannot be ' +
        'undone.',
      confirmText: 'Delete',
      cancelText: 'Cancel',
      destructive: true,
    };
    const dialogRef = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data,
    });
    const confirmed = await firstValueFrom(dialogRef.closed);
    if (!confirmed) return;

    this.mutating.set(true);
    try {
      await this.artifactHttp.deleteArtifact(a.artifactId);
      this.artifactState.remove(a.artifactId);
    } catch {
      this.toast.error(
        'Could not delete artifact',
        'Nothing was removed. Try again in a moment.',
      );
    } finally {
      this.mutating.set(false);
    }
  }

  protected async download(): Promise<void> {
    if (this.downloading()) return;
    const a = this.artifact();
    this.downloading.set(true);
    try {
      await this.artifactDownload.download({
        artifactId: a.artifactId,
        version: a.version,
      });
    } finally {
      this.downloading.set(false);
    }
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

  return parseIso(then).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
  });
}
