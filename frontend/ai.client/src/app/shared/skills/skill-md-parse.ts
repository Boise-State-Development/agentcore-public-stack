/**
 * The server's reading of one `SKILL.md` (`POST /skills/mine/import/parse` and
 * `POST /admin/skills/import/parse`). The SPA never parses the format itself:
 * both "Import SKILL.md" controls post the file and patch their form from this.
 * Mirrors `SkillMdParseResponse` in `apis/shared/skills/skill_md_import.py`.
 */

/** One note about a `SKILL.md` or bundle. Errors block a save; warnings do not. */
export interface SkillMdDiagnostic {
  level: 'error' | 'warning';
  /** The frontmatter key or file it concerns; `''` for the file as a whole. */
  field: string;
  code: string;
  message: string;
}

export interface SkillMdParseResult {
  /** Frontmatter `name`, or `''`. */
  name: string;
  /** `name` humanised (`pdf-tools` → `Pdf tools`), or `''` with no name. */
  displayName: string;
  description: string;
  instructions: string;
  /** Advisory only; a skill never grants a tool. */
  allowedTools: string[];
  /** Every other frontmatter key, nested values intact. */
  skillMetadata: Record<string, unknown>;
  diagnostics: SkillMdDiagnostic[];
}

/** The server's 400 `detail` when the file could not be read, else a fallback. */
export function skillMdParseErrorMessage(err: unknown): string {
  if (err && typeof err === 'object' && 'error' in err) {
    const body = (err as { error?: unknown }).error;
    if (body && typeof body === 'object' && 'detail' in body) {
      const detail = (body as { detail?: unknown }).detail;
      if (typeof detail === 'string') return detail;
    }
  }
  return 'Could not read the selected SKILL.md file.';
}
