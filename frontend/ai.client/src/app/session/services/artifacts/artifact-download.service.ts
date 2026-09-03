import { Injectable, inject } from '@angular/core';
import { ArtifactHttpService } from './artifact-http.service';
import { ArtifactShareService } from './artifact-share.service';
import { SessionService } from '../session/session.service';
import { ToastService } from '../../../services/toast/toast.service';

/**
 * Which artifact version to save, and by what authority.
 *
 * An owner identifies the version directly. A recipient has no artifact
 * id they are allowed to mint against, so they pass `shareId` instead
 * and the mint goes through the access-checked share endpoint.
 */
export type DownloadableArtifact =
  | { artifactId: string; version: number; shareId?: undefined }
  | { shareId: string; artifactId?: undefined; version?: undefined };

/**
 * Saves an artifact version to disk. Shared by the inline card and the
 * docked panel so the credential handling stays in exactly one place.
 *
 * The content only lives on the artifact origin (no CORS,
 * `connect-src 'none'`), so the SPA can't fetch the bytes into a blob.
 * Instead it mints a fresh single-use render token and points a
 * throwaway hidden iframe at the render URL with `download=1`; the
 * render Lambda answers that with `Content-Disposition: attachment`, so
 * the browser saves the file without navigating this document. A
 * bad/expired token lands as an error page inside the discarded iframe,
 * never the SPA.
 */
@Injectable({ providedIn: 'root' })
export class ArtifactDownloadService {
  private artifactHttp = inject(ArtifactHttpService);
  private artifactShares = inject(ArtifactShareService);
  private sessionService = inject(SessionService);
  private toast = inject(ToastService);

  /** Mint a token and kick off the save. Surfaces a toast and resolves
   *  `false` on failure so callers can clear their own busy state.
   *
   *  Both mint paths return the same `{url}` shape, so everything below
   *  the mint — the `?download=1` suffix and the hidden iframe — is
   *  identical for an owner and a recipient. */
  async download(ref: DownloadableArtifact): Promise<boolean> {
    try {
      // `!== undefined` (not truthiness) so the union discriminates:
      // it is what narrows the else branch to the owner variant.
      const token =
        ref.shareId !== undefined
          ? await this.artifactShares.mintSharedRenderToken(ref.shareId)
          : await this.artifactHttp.mintRenderToken(
              ref.artifactId,
              ref.version,
              this.sessionService.currentSession().sessionId,
            );
      const sep = token.url.includes('?') ? '&' : '?';
      this.trigger(`${token.url}${sep}download=1`);
      return true;
    } catch {
      this.toast.error(
        'Download failed',
        "This artifact couldn't be downloaded. It may have expired or been removed.",
      );
      return false;
    }
  }

  private trigger(url: string): void {
    if (typeof document === 'undefined') return;
    const frame = document.createElement('iframe');
    frame.setAttribute('aria-hidden', 'true');
    frame.style.display = 'none';
    frame.src = url;
    document.body.appendChild(frame);
    // The token is single-use and short-lived; the save kicks off on
    // load. Keep the frame briefly so the transfer can start, then drop
    // it so the credential URL doesn't linger in the DOM.
    setTimeout(() => frame.remove(), 60_000);
  }
}
