import { Injectable, inject } from '@angular/core';
import { ArtifactHttpService } from './artifact-http.service';
import { SessionService } from '../session/session.service';
import { ToastService } from '../../../services/toast/toast.service';

/** Minimal identity of the artifact version to save. */
export interface DownloadableArtifact {
  artifactId: string;
  version: number;
}

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
  private sessionService = inject(SessionService);
  private toast = inject(ToastService);

  /** Mint a token and kick off the save. Surfaces a toast and resolves
   *  `false` on failure so callers can clear their own busy state. */
  async download(ref: DownloadableArtifact): Promise<boolean> {
    try {
      const sessionId = this.sessionService.currentSession().sessionId;
      const token = await this.artifactHttp.mintRenderToken(
        ref.artifactId,
        ref.version,
        sessionId,
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
