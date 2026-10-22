/**
 * Client mirror of the skill-resource type allowlist.
 *
 * Canonical source: `backend/src/apis/shared/skills/resource_types.py`
 * (`SAFE_EXTENSION_CONTENT_TYPES`). **The server is the control** — it rejects a
 * disallowed upload with a 400 whatever the client does. This module exists so
 * the file picker can filter and a rejected file produces an immediate,
 * specific message instead of a failed round-trip.
 *
 * Web-document extensions (`.html`, `.htm`, `.xhtml`, `.xml`, `.svg`) are
 * absent deliberately. A skill resource is uploaded by one user and downloaded
 * by others from **this app's own origin**, so a resource a browser renders as
 * a document could execute script inside a reader's authenticated session.
 * Source-code extensions are allowed but are stored and served as plain text.
 *
 * Keep this list in sync with the Python allowlist when either changes.
 */

/** Extensions (without the dot) the backend accepts for a skill resource. */
export const ALLOWED_RESOURCE_EXTENSIONS: readonly string[] = [
  // Prose / structured text
  'md', 'markdown', 'txt', 'text', 'csv', 'tsv', 'json', 'jsonl', 'yaml', 'yml',
  'toml', 'ini', 'cfg', 'env', 'log', 'rst', 'tex',
  // Example / helper code — stored and served as inert plain text
  'py', 'sh', 'bash', 'zsh', 'ps1', 'rb', 'pl', 'r', 'js', 'mjs', 'cjs', 'ts',
  'jsx', 'tsx', 'css', 'sql', 'graphql', 'java', 'kt', 'go', 'rs', 'c', 'h',
  'cpp', 'hpp', 'cs', 'swift', 'php', 'lua', 'dockerfile', 'diff', 'patch',
  // Documents
  'pdf', 'docx', 'xlsx', 'pptx',
  // Raster images only — SVG is a scriptable document
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico',
  // Archives
  'zip', 'gz', 'tar',
];

/** Value for the `accept` attribute of a skill-resource file input. */
export const RESOURCE_ACCEPT_ATTR = ALLOWED_RESOURCE_EXTENSIONS.map(
  (ext) => `.${ext}`,
).join(',');

/**
 * True when a filename's extension is one the backend will accept.
 *
 * Resolves on the LAST extension, matching the server, so `notes.md.html` is
 * correctly treated as HTML rather than markdown. An extension-less name is
 * accepted only if the whole name is itself a known type key (`Dockerfile`).
 */
export function isAllowedResourceFilename(filename: string): boolean {
  const name = (filename ?? '').trim().toLowerCase();
  if (!name) return false;
  const dot = name.lastIndexOf('.');
  const key = dot === -1 ? name : name.slice(dot + 1);
  return ALLOWED_RESOURCE_EXTENSIONS.includes(key);
}

/** The message shown when a file is refused, shared by both skill forms. */
export const DISALLOWED_RESOURCE_MESSAGE =
  'is not an accepted file type. Skill files must be documents, data, images, ' +
  'or plain-text code — web documents such as .html and .svg are not accepted ' +
  "because they can run script in a reader's browser.";
