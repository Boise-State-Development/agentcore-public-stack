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
  heroDocument,
  heroDocumentText,
  heroPhoto,
  heroTableCells,
} from '@ng-icons/heroicons/outline';
import type { Artifact } from '../../../../services/artifacts/artifact.model';
import { ArtifactStateService } from '../../../../services/artifacts/artifact-state.service';
import { ArtifactHttpService } from '../../../../services/artifacts/artifact-http.service';
import { ArtifactDownloadService } from '../../../../services/artifacts/artifact-download.service';
import { SessionService } from '../../../../services/session/session.service';
import {
  ArtifactShareModalComponent,
  type ArtifactShareModalData,
} from './artifact-share-modal.component';
import { UserService } from '../../../../../auth/user.service';
import { ToastService } from '../../../../../services/toast/toast.service';
import {
  ConfirmationDialogComponent,
  type ConfirmationDialogData,
} from '../../../../../components/confirmation-dialog';
import {
  CONTENT_CARD_ACTION_CLASS,
  ContentCardComponent,
  type ContentCardMenuItem,
} from '../../../../../components/content-card/content-card.component';
import {
  RenameArtifactDialogComponent,
  type RenameArtifactDialogData,
  type RenameArtifactDialogResult,
} from '../../../../../artifacts/components/rename-artifact-dialog.component';
import {
  ArtifactThumbnailComponent,
  type ArtifactThumbnailSource,
} from '../../../../../artifacts/components/artifact-thumbnail.component';
import { artifactTypeStyle } from '../../../../../artifacts/artifact-type-style';
import { parseIso } from '../../../../../utils/date';

/** The overflow menu's actions. */
type ArtifactCardAction = 'share' | 'download' | 'rename' | 'delete';

/**
 * One artifact version in the conversation, drawn with the shared content
 * card the browser and attachment cards use, so a page, a file and an
 * artifact read as one family: a live miniature of the artifact in the
 * tray, then the title, a `Type · vN · when` line, Open, and an overflow
 * menu.
 *
 * The miniature is `ArtifactThumbnailComponent`, the library grid's
 * preview: the artifact rendered at a desktop width in the same sandboxed,
 * null-origin, `inert` frame and scaled down. It mints lazily, once, when
 * the card nears the viewport. The artifact's content is never inlined
 * here.
 *
 * The menu keeps the old card's distinction between scopes: Share and
 * Download act on the version this card shows (one card per version), and
 * say so; Rename and Delete act on the whole artifact.
 *
 * Anchored inline after its producing assistant message by the
 * message-list (via `Artifact.producedByMessageIndex`); only legacy /
 * unanchorable artifacts fall back to the end-of-conversation strip.
 */
@Component({
  selector: 'app-artifact-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ContentCardComponent, ArtifactThumbnailComponent, NgIcon],
  providers: [
    provideIcons({
      heroCodeBracket,
      heroDocument,
      heroDocumentText,
      heroPhoto,
      heroTableCells,
    }),
  ],
  host: { class: 'block w-80 max-w-full' },
  template: `
    <app-content-card
      [title]="title()"
      [label]="cardLabel()"
      [meta]="meta()"
      [status]="status()"
      [live]="downloading()"
      [menuItems]="menuItems()"
      (menuSelect)="onMenu($event)"
    >
      <div contentCardChrome class="flex min-w-0 flex-1 items-center gap-1.5" aria-hidden="true">
        <span class="flex shrink-0" [class]="style().text">
          <ng-icon [name]="style().icon" size="13" />
        </span>
        <span
          class="min-w-0 flex-1 truncate rounded-full bg-gray-100 px-2 text-[10px]/4 text-gray-600 dark:bg-white/5 dark:text-gray-400"
          >{{ style().label }}</span
        >
      </div>

      <app-artifact-thumbnail contentCardPreview [source]="thumbnail()" />

      <button
        contentCardAction
        type="button"
        [class]="actionClass"
        [attr.aria-label]="ariaLabel()"
        (click)="open()"
      >
        Open
      </button>
    </app-content-card>
  `,
})
export class ArtifactCardComponent {
  artifact = input.required<Artifact>();

  private artifactState = inject(ArtifactStateService);
  private artifactHttp = inject(ArtifactHttpService);
  private artifactDownload = inject(ArtifactDownloadService);
  private sessionService = inject(SessionService);
  private dialog = inject(Dialog);
  private userService = inject(UserService);
  private toast = inject(ToastService);

  protected readonly actionClass = CONTENT_CARD_ACTION_CLASS;

  protected readonly downloading = signal(false);
  /** A rename or delete is in flight, so both of those wait. */
  protected readonly mutating = signal(false);

  protected readonly style = computed(() =>
    artifactTypeStyle(this.artifact().contentType),
  );

  protected readonly title = computed(
    () => this.artifact().title || 'Untitled artifact',
  );

  /** Short, human relative time. Empty when the timestamp is missing or
   *  unparseable, so the line just omits it. */
  protected readonly updatedLabel = computed<string>(() =>
    relativeTime(this.artifact().updatedAt),
  );

  protected readonly meta = computed(
    () => `${this.style().label} · v${this.artifact().version}`,
  );

  protected readonly status = computed(() =>
    this.downloading() ? 'Downloading…' : this.updatedLabel(),
  );

  /** The version this card shows, in the conversation that made it. */
  protected readonly thumbnail = computed<ArtifactThumbnailSource>(() => {
    const a = this.artifact();
    return {
      kind: 'owned',
      artifactId: a.artifactId,
      version: a.version,
      sessionId: this.sessionService.currentSession().sessionId,
      contentType: a.contentType,
    };
  });

  protected readonly cardLabel = computed(
    () =>
      `${this.style().label} artifact ${this.title()}, version ${this.artifact().version}`,
  );

  /** Contains the visible "Open" (WCAG 2.5.3) and names what it opens. */
  protected readonly ariaLabel = computed(
    () =>
      `Open ${this.style().label} artifact ${this.artifact().title || 'Untitled'}, version ${this.artifact().version}`,
  );

  /* Share and Download name the version, because the card does; Rename
     and Delete name none, deliberately, and Delete says "and all
     versions" in its confirmation. This card is one of possibly several
     for the same artifact, each captioned with its own version, so
     "Delete version 2" beside "Download version 2" would promise
     something the control does not do: both act on the whole artifact. */
  protected readonly menuItems = computed<ContentCardMenuItem[]>(() => {
    const v = this.artifact().version;
    return [
      { id: 'share', label: `Share version ${v}` },
      { id: 'download', label: `Download version ${v}` },
      { id: 'rename', label: 'Rename artifact' },
      { id: 'delete', label: 'Delete artifact', danger: true },
    ];
  });

  protected onMenu(id: string): void {
    switch (id as ArtifactCardAction) {
      case 'share':
        this.share();
        return;
      case 'download':
        void this.download();
        return;
      case 'rename':
        void this.rename();
        return;
      case 'delete':
        void this.confirmDelete();
        return;
    }
  }

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
    if (this.mutating()) return;
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
   *  and Download items really are scoped to that version. The dialog
   *  is the last chance to correct that reading before the other cards
   *  vanish alongside this one.
   *
   *  Removal goes through the registry, which is what every sibling card
   *  and the docked panel read — one call clears them all. Only after
   *  the request succeeds: an optimistic removal would look like success
   *  and then reappear on the next session load. */
  protected async confirmDelete(): Promise<void> {
    if (this.mutating()) return;
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
