/**
 * Durable download links for user-files objects.
 *
 * Presigned S3 URLs expire in minutes. Anything that writes one into a
 * conversation — the inline download card on a generated .docx, or a link the
 * model composed from a tool result — is dead by the time the thread is
 * reopened, and worse: a model re-emitting a ~1,400-character signed URL in
 * prose truncates it at the `?`, producing an unsigned link that S3 answers
 * with `AccessDenied` even while the card's own button still works.
 *
 * The fix is to never render a signed URL. `downloadUrlFor` builds the stable,
 * cookie-authed app-api path, which mints a fresh presigned URL per click and
 * 302s to it. `durableDownloadUrlFromHref` recovers that same path from an
 * already-persisted raw S3 URL, so conversations written before the backend
 * stopped emitting signed URLs heal on render.
 */

/**
 * Object keys in the user-files bucket are
 * `user-files/{userId}/{sessionId}/{uploadId}/{filename}` — see
 * `apis/shared/files/workspace.py` and `agents/builtin_tools/office/_storage.py`,
 * which both compose the key that way. The upload id is the fourth segment.
 */
const USER_FILES_KEY = /^\/?user-files\/[^/]+\/[^/]+\/([^/]+)\/[^/]+$/;

/** Hosts that serve the user-files bucket directly (virtual-hosted style). */
const S3_HOST = /(^|\.)s3[.-][a-z0-9-]+\.amazonaws\.com$/i;

/** The durable, owner-scoped download path for an uploaded file. */
export function downloadUrlFor(appApiUrl: string, uploadId: string): string {
  return `${appApiUrl}/files/${encodeURIComponent(uploadId)}/download`;
}

/**
 * Recover the upload id from a raw user-files S3 URL.
 *
 * Returns `null` for every other href — including S3 URLs that don't match the
 * user-files key shape — so callers leave unrelated links untouched.
 *
 * Split out from `durableDownloadUrlFromHref` because the preview pane needs
 * the id itself, not a download link: `/files/{id}/preview-url` is a different
 * route, and re-parsing the href a second way would be two places to get the
 * key layout wrong.
 */
export function uploadIdFromHref(href: string): string | null {
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return null;
  }

  if (url.protocol !== 'https:' || !S3_HOST.test(url.hostname)) return null;

  const uploadId = USER_FILES_KEY.exec(url.pathname)?.[1];
  return uploadId ? decodeURIComponent(uploadId) : null;
}

/**
 * Rewrite a raw user-files S3 URL to the durable download route.
 *
 * Returns `null` for every other href — including S3 URLs that don't match the
 * user-files key shape — so callers leave unrelated links untouched.
 */
export function durableDownloadUrlFromHref(
  appApiUrl: string,
  href: string,
): string | null {
  const uploadId = uploadIdFromHref(href);
  return uploadId ? downloadUrlFor(appApiUrl, uploadId) : null;
}
