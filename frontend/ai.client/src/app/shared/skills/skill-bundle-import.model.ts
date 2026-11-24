/**
 * Skill bundle import — the wire types both tiers share
 * (docs/specs/skill-bundle-import.md). My Skills (`/skills/mine/import*`) and
 * the admin catalog (`/admin/skills/import*`) answer with the same preview,
 * built by one backend module (`apis/app_api/skills/bundle_import_core.py`);
 * only the skill an apply returns differs, so `ImportApplyResult` is generic.
 */

import { SkillMdDiagnostic } from './skill-md-parse';

/** Where a skill was last imported from. Display and refresh only; never versioned. */
export interface SkillImportSource {
  type: 'zip' | 'github';
  /** The bundle's directory inside the archive ('' is the top level). */
  path: string;
  bundleHash: string;
  importedAt: string;
  url?: string | null;
  owner?: string | null;
  repo?: string | null;
  ref?: string | null;
  commitSha?: string | null;
  filename?: string | null;
  /** The frontmatter `name` as the bundle wrote it. */
  originalName?: string | null;
}

/** What an import does to one file, against the skill it would write. */
export type ImportFileStatus = 'added' | 'changed' | 'unchanged' | 'removed' | 'skipped';

export interface ImportFileRow {
  /** The path as the bundle has it (a removed file's is its bundle path here). */
  path: string;
  status: ImportFileStatus;
  filename: string | null;
  kind: 'reference' | 'script' | 'asset' | null;
  size: number;
  /** Why a `skipped` file was left out. */
  reason: string | null;
}

/** `POST …/import/preview` (either tier). With `candidates`, no skill was chosen yet. */
export interface ImportPreview {
  source: SkillImportSource;
  candidates: string[];
  skill: {
    name: string;
    /** The activation name the skill will have; can differ from `name` (a diagnostic says why). */
    storedName: string;
    displayName: string;
    description: string;
    instructionsLength: number;
    allowedTools: string[];
  } | null;
  /** Against the target skill; null when creating. */
  changes: {
    description: boolean;
    instructions: boolean;
    allowedTools: boolean;
    skillMetadata: boolean;
  } | null;
  files: ImportFileRow[];
  bundleHash: string | null;
  targetUpdatedAt: string | null;
  /** Against the target skill only (a re-import or refresh); null when creating. */
  descriptionDiff?: ImportTextDiff | null;
  instructionsDiff?: ImportTextDiff | null;
  /** The commit the target skill was last imported from, beside `source.commitSha`. */
  previousCommitSha?: string | null;
  /** Frontmatter rules, the colon repair, unsafe characters, a renamed skill. */
  diagnostics?: SkillMdDiagnostic[];
}

/** A unified diff of one field (`''` when unchanged), cut at 400 lines. */
export interface ImportTextDiff {
  diff: string;
  truncated: boolean;
}

/** An applied import: the skill as written (each tier's own shape) and its file rows. */
export interface ImportApplyResult<TSkill> {
  skill: TSkill;
  files: ImportFileRow[];
}

/** A bundle to import: a `.zip` or a public GitHub URL, and optionally its path. */
export interface SkillImportInput {
  file?: File | null;
  url?: string | null;
  /** Sent only when chosen; `''` names the archive's top level. */
  path?: string | null;
}

/** Uploaded `.zip` ceiling, mirroring the backend `MAX_UPLOAD_ARCHIVE_BYTES`. */
export const MAX_IMPORT_ZIP_BYTES = 10 * 1024 * 1024;

/**
 * What `SkillBundleImportComponent` needs from a tier: the three import routes
 * and its error copy. `MySkillService` and `AdminSkillService` both implement it.
 */
export interface SkillBundleImportApi<TSkill> {
  /** What importing `input` would do, against `skillId` when given. Writes nothing. */
  previewImport(input: SkillImportInput, skillId?: string | null): Promise<ImportPreview>;
  /** Create a skill from a bundle. `expectedBundleHash` is the preview's. */
  importSkill(
    input: SkillImportInput,
    options: { displayName?: string; expectedBundleHash?: string | null },
  ): Promise<ImportApplyResult<TSkill>>;
  /** Re-import into an existing skill; `confirmReach` only means anything to My Skills. */
  reimportSkill(
    skillId: string,
    input: SkillImportInput,
    options: {
      expectedBundleHash?: string | null;
      expectedUpdatedAt?: string | null;
      confirmReach?: boolean;
    },
  ): Promise<ImportApplyResult<TSkill>>;
  /** The backend's message for a failed call, or `fallback`. */
  describeError(err: unknown, fallback: string): string;
}

/** The multipart body every import route takes: a `.zip` or a URL, and the chosen path. */
export function skillImportForm(input: SkillImportInput): FormData {
  const form = new FormData();
  if (input.file) {
    form.append('file', input.file, input.file.name);
  } else if (input.url) {
    form.append('url', input.url.trim());
  }
  if (input.path !== undefined && input.path !== null) {
    form.append('path', input.path);
  }
  return form;
}

/**
 * FastAPI's message for a failed call (`error.detail` as a string, or its
 * `message`), else the error's own message, else `fallback`.
 */
export function describeSkillApiError(err: unknown, fallback: string): string {
  const detail = (err as { error?: { detail?: unknown } } | null)?.error?.detail;
  if (typeof detail === 'string' && detail.trim()) {
    return detail;
  }
  const message = (detail as { message?: unknown } | null)?.message;
  if (typeof message === 'string' && message.trim()) {
    return message;
  }
  return err instanceof Error ? err.message : fallback;
}
