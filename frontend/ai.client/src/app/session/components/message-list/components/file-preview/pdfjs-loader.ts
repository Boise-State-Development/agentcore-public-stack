import { Injectable } from '@angular/core';

export type PdfJs = typeof import('pdfjs-dist');

/** The data folders `angular.json` copies to `/pdfjs/<folder>/`. */
export type PdfJsAssetFolder = 'cmaps' | 'standard_fonts' | 'wasm' | 'iccs';

/**
 * Loads pdf.js on first use and points it at its worker.
 *
 * A service rather than an import inside `PdfViewerComponent` so specs can
 * provide a stand-in through TestBed: jsdom has no canvas and no module
 * workers, so the real library cannot run under test.
 *
 * The worker is bundled, not copied: `new Worker(new URL(…, import.meta.url))`
 * is the form the Angular builder follows (see `pdf.worker.ts`), so the
 * worker gets a content hash like every other chunk and the deploy pins it
 * as immutable. A copied, unhashed worker could be a newer build than the
 * library a tab already holds after a deploy, and pdf.js refuses to run on
 * a version mismatch.
 *
 * One worker for the whole tab (`workerPort`), created only when the first
 * PDF is opened. pdf.js keeps it alive between documents, so reopening the
 * pane does not pay the worker's start-up again.
 */
@Injectable({ providedIn: 'root' })
export class PdfJsLoader {
  private library: Promise<PdfJs> | null = null;

  load(): Promise<PdfJs> {
    this.library ??= import('pdfjs-dist').then(
      (pdfjs) => {
        pdfjs.GlobalWorkerOptions.workerPort ??= new Worker(
          new URL('./pdf.worker', import.meta.url),
          { type: 'module' },
        );
        return pdfjs;
      },
      (error: unknown) => {
        // A failed chunk load (a dropped connection, a deploy that removed
        // the old chunk) must not be cached, or every later preview in the
        // tab fails without trying again.
        this.library = null;
        throw error;
      },
    );
    return this.library;
  }

  /**
   * URL of one of pdf.js's data folders, resolved against the document's
   * base so it works under any `<base href>`. pdf.js wants the trailing
   * slash.
   *
   * These are fetched only when a document needs them: `cmaps` for CJK
   * text, `standard_fonts` for the 14 standard fonts a PDF may name without
   * embedding, `wasm` for JPEG 2000 and JBIG2 images (common in scans), and
   * `iccs` for colour profiles.
   */
  assetUrl(folder: PdfJsAssetFolder): string {
    return new URL(`pdfjs/${folder}/`, document.baseURI).href;
  }
}
