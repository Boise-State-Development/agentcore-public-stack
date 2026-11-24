import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowDownTray,
  heroCodeBracket,
  heroDocument,
  heroDocumentText,
  heroPhoto,
  heroPresentationChartBar,
  heroTableCells,
} from '@ng-icons/heroicons/outline';
import { ConfigService } from '../../../../../../services/config.service';
import { resolveMimeTypeFor } from '../../../../../../services/file-upload';
import {
  CONTENT_CARD_ACTION_CLASS,
  CONTENT_CARD_COMPACT_ACTION_CLASS,
  ContentCardComponent,
  OUTPUT_CARD_HOST_CLASS,
} from '../../../../../../components/content-card/content-card.component';
import {
  downloadUrlFor,
  uploadIdFromHref,
} from '../../../../../../shared/utils/file-download-url';
import { FilePreviewStateService } from '../../../../../services/file-preview/file-preview-state.service';
import { isPreviewableFilename } from '../../../../../services/file-preview/file-preview.model';
import { attachmentKind, type AttachmentKind } from '../../../../attachment-card/attachment-kind';
import { AttachmentPreviewComponent } from '../../../../attachment-card/attachment-preview.component';
import { ImageOutputCardComponent } from './image-output-card.component';

/**
 * Payload for the file_download inline visual, produced by the office document
 * tools (create_word_document, modify_word_document, create_excel_spreadsheet,
 * ...) and by workspace_write.
 *
 * `upload_id` is the current contract; the card resolves it to the durable
 * `/files/{uploadId}/download` route, which mints a presigned URL per click.
 * `download_url` is the legacy field — a presigned S3 URL that expired an hour
 * after the message was written — and is kept only so cards already persisted
 * in old conversations still work: the upload id is recovered from the S3 key
 * and routed the same way.
 */
interface FileDownloadPayload {
  filename: string;
  upload_id?: string;
  download_url?: string;
  size_kb?: string;
}

interface ResolvedFile {
  filename: string;
  href: string;
  uploadId: string;
  previewable: boolean;
  mimeType: string;
  kind: AttachmentKind;
  meta: string;
}

/**
 * The file a tool made (a Word document, a spreadsheet, a deck, a workspace
 * file), drawn with the shared content card the attachment, artifact and
 * browser cards use, so a file the user attached and a file the agent made
 * read as one family: its preview in the tray, the type in the chrome row,
 * then the name and a `TYPE · size` line.
 *
 * Placed under the tool rail that made it (the call itself folds into the
 * rail), so the download is never hidden inside a collapsed rail.
 *
 * The primary action is a compact Preview, rightmost, where the docked pane
 * renders the format, with a Download icon before it; otherwise Download is
 * the action. Download is a
 * real link to the durable route, which 302s to a fresh presigned URL. The
 * chat's prose rules stop at a content card (`@scope` in styles.css), so the
 * links take the card's own colours rather than the answer's link style.
 *
 * An image is the exception: there is nothing to preview that the card could
 * not simply show, so it is drawn whole, and opens in the lightbox, by
 * `ImageOutputCardComponent`.
 */
@Component({
  selector: 'app-file-download-renderer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ContentCardComponent, AttachmentPreviewComponent, ImageOutputCardComponent, NgIcon],
  providers: [
    provideIcons({
      heroArrowDownTray,
      heroCodeBracket,
      heroDocument,
      heroDocumentText,
      heroPhoto,
      heroPresentationChartBar,
      heroTableCells,
    }),
  ],
  host: { class: OUTPUT_CARD_HOST_CLASS },
  template: `
    @if (file(); as f) {
      @if (f.kind.preview === 'image') {
        <app-image-output-card
          [uploadId]="f.uploadId"
          [filename]="f.filename"
          [href]="f.href"
          [meta]="f.meta"
        />
      } @else {
        <app-content-card size="lg" [title]="f.filename" [meta]="f.meta">
          <div contentCardChrome class="flex min-w-0 flex-1 items-center gap-1.5" aria-hidden="true">
            <span class="flex shrink-0" [class]="f.kind.accent">
              <ng-icon [name]="f.kind.icon" size="13" />
            </span>
            <span
              class="min-w-0 flex-1 truncate rounded-full bg-gray-100 px-2 text-[10px]/4 text-gray-600 dark:bg-white/5 dark:text-gray-400"
              >{{ f.kind.name }}</span
            >
          </div>

          <app-attachment-preview contentCardPreview [uploadId]="f.uploadId" [mimeType]="f.mimeType" />

          <!-- One container per branch: a slot only projects a control-flow
               block whose root is a single node matching it. -->
          @if (f.previewable) {
            <ng-container contentCardAction>
            <a
              class="flex size-8 shrink-0 items-center justify-center rounded-lg text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-100"
              [href]="f.href"
              [attr.download]="f.filename"
              [attr.aria-label]="'Download ' + f.filename"
              [title]="'Download'"
              rel="noopener noreferrer"
            >
              <ng-icon name="heroArrowDownTray" size="18" aria-hidden="true" />
            </a>
            <button
              type="button"
              [class]="compactActionClass"
              [attr.aria-label]="'Preview ' + f.filename"
              (click)="preview(f.uploadId, f.filename)"
            >
              Preview
            </button>
            </ng-container>
          } @else {
            <a
              contentCardAction
              [class]="actionClass"
              [href]="f.href"
              [attr.download]="f.filename"
              [attr.aria-label]="'Download ' + f.filename"
              rel="noopener noreferrer"
            >
              Download
            </a>
          }
        </app-content-card>
      }
    }
  `,
})
export class FileDownloadRendererComponent {
  /** The payload data from the backend tool result. */
  payload = input.required<unknown>();

  private readonly config = inject(ConfigService);
  private readonly filePreview = inject(FilePreviewStateService);

  protected readonly actionClass = CONTENT_CARD_ACTION_CLASS;
  protected readonly compactActionClass = CONTENT_CARD_COMPACT_ACTION_CLASS;

  /** Narrowed, validated payload with its kind resolved (null when malformed). */
  file = computed<ResolvedFile | null>(() => {
    const raw = this.payload();
    if (!raw || typeof raw !== 'object') return null;
    const p = raw as Partial<FileDownloadPayload>;
    if (!p.filename) return null;

    // Legacy cards carry only an expired presigned S3 URL; the upload id
    // is still recoverable from its key, and preview needs the id rather
    // than the link, so resolve it once and derive both from it.
    const uploadId = p.upload_id ?? (p.download_url ? uploadIdFromHref(p.download_url) : null);
    if (!uploadId) return null;

    const mimeType = resolveMimeTypeFor(p.filename, '');
    const kind = attachmentKind(mimeType);
    return {
      filename: p.filename,
      href: downloadUrlFor(this.config.appApiUrl(), uploadId),
      uploadId,
      previewable: isPreviewableFilename(p.filename),
      mimeType,
      kind,
      meta: p.size_kb ? `${kind.label} · ${p.size_kb}` : kind.label,
    };
  });

  /** Open the docked preview pane on this file. */
  protected preview(uploadId: string, filename: string): void {
    this.filePreview.open({ uploadId, filename });
  }
}
