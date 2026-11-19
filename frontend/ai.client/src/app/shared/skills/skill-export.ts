import { HttpErrorResponse } from '@angular/common/http';

/**
 * The bundle directory and archive stem an export uses: the backend's
 * `slugify_skill_name` (`apis/shared/skills/bundle.py`), so the saved file is
 * named exactly what the server's `Content-Disposition` says.
 */
export function skillExportSlug(skillId: string): string {
  const slug = skillId
    .trim()
    .toLowerCase()
    .replace(/_/g, '-')
    .replace(/[^a-z0-9-]+/g, '-')
    .replace(/-{2,}/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64)
    .replace(/^-+|-+$/g, '');
  return slug || 'skill';
}

export function skillExportFilename(skillId: string): string {
  return `${skillExportSlug(skillId)}.zip`;
}

/**
 * How long the object URL outlives the click. Safari and Firefox read the URL
 * after the click returns, so revoking it in the same tick can cancel the save
 * or produce an empty file.
 */
export const SAVE_BLOB_REVOKE_DELAY_MS = 10_000;

/** Hand a downloaded body to the browser's save flow. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.style.display = 'none';
  // Firefox ignores a click on a link that is not in the document.
  document.body.appendChild(link);
  try {
    link.click();
  } finally {
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), SAVE_BLOB_REVOKE_DELAY_MS);
  }
}

/**
 * A readable message for a failed export. The request asks for a `Blob`, so an
 * error body (the 409 that names an unreadable file) arrives as one too.
 */
export async function skillExportErrorMessage(err: unknown): Promise<string> {
  const fallback = "Couldn't download the skill. Please try again.";
  if (!(err instanceof HttpErrorResponse)) return fallback;
  let body: unknown = err.error;
  if (body instanceof Blob) {
    try {
      body = JSON.parse(await body.text());
    } catch {
      return fallback;
    }
  }
  const detail = (body as { detail?: unknown } | null)?.detail;
  return typeof detail === 'string' && detail ? detail : fallback;
}
