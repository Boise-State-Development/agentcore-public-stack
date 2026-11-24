/**
 * pdf.js's worker, as a module of ours so the Angular builder bundles it.
 *
 * The builder follows `new Worker(new URL('./pdf.worker', import.meta.url))`
 * only for a relative path, so `pdfjs-loader.ts` points here and this file
 * pulls in the library's worker build. That build starts itself when it
 * loads in a worker scope, so importing it is all there is to do.
 */
import 'pdfjs-dist/build/pdf.worker.min.mjs';
