/**
 * Where a skill file sits in its bundle, below its kind directory.
 *
 * A file at the top of `references/` (every file uploaded before nested paths)
 * has a `path` equal to its `filename`, and older responses may carry no `path`
 * at all, so both read the same. A file imported into a subfolder has a `path`
 * such as `legal/nda.md` while its `filename` is `nda.md`.
 */
export interface SkillFileLike {
  filename: string;
  path?: string | null;
}

/** The file's path below its kind directory (`legal/nda.md`, or `forms.md`). */
export function skillFilePath(file: SkillFileLike): string {
  return file.path || file.filename;
}

/** Whether the file sits in a subfolder of its kind directory. */
export function isNestedSkillFile(file: SkillFileLike): boolean {
  return skillFilePath(file).includes('/');
}

/**
 * A path as a URL suffix: each segment encoded, joined by `/`. A top-level
 * file's URL is exactly `encodeURIComponent(filename)`, as before.
 */
export function encodeSkillFilePath(path: string): string {
  return path.split('/').map(encodeURIComponent).join('/');
}
