/**
 * My Skills models — the TypeScript mirror of the backend
 * `apis/app_api/skills/routes.py` My Skills DTOs (`MySkillResponse`,
 * `CreateMySkillRequest`, `UpdateMySkillRequest`). Shapes must stay in sync
 * with that module (CLAUDE.md cross-package contract).
 *
 * A skill here is a *pure knowledge bundle* in the agentskills.io format —
 * instructions plus supporting files, no tool bindings (Skills v2 D1). Tools,
 * models and knowledge bind on the Agent, never on a skill.
 */

import { SkillMdDiagnostic } from '../../../shared/skills/skill-md-parse';

import { SkillStatus } from '../../../admin/skills/models/admin-skill.model';

export type { SkillStatus };

/**
 * Which directory of the bundle a supporting file lives in. `script` files are
 * accept-and-inert: stored and readable by the agent, never executed (D5).
 */
export type SkillResourceKind = 'reference' | 'script' | 'asset';

/**
 * Manifest entry for one supporting file. Bytes live in S3; this is the
 * lightweight pointer carried on the skill row.
 */
export interface MySkillResourceRef {
  filename: string;
  contentHash: string;
  size: number;
  contentType: string;
  s3Key: string;
  kind: SkillResourceKind;
  /** Path below the kind directory (`legal/nda.md`); equals `filename` for a top-level file. */
  path?: string;
}

/**
 * One skill the current user authored.
 */
export interface MySkill {
  skillId: string;
  displayName: string;
  description: string;
  instructions: string;
  /** Advisory only — parsed from SKILL.md frontmatter, never enforced (D4). */
  allowedTools: string[];
  /** agentskills.io frontmatter passthrough (license, compatibility, ...). */
  skillMetadata: Record<string, unknown>;
  resources: MySkillResourceRef[];
  status: SkillStatus;
  category: string | null;
  createdAt: string | null;
  updatedAt: string | null;
  /** Set once the skill was imported from a bundle (skill-bundle-import.md §3). */
  importSource?: SkillImportSource | null;
  /**
   * `"workflow"` for a saved workflow's carrying skill (workflow-designer.md §20.3, PR-D.1):
   * Customize marks it Workflow and links it to its designer. Null for any other skill.
   */
  kind?: 'workflow' | null;
}

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
  kind: SkillResourceKind | null;
  size: number;
  /** Why a `skipped` file was left out. */
  reason: string | null;
}

/** `POST /skills/mine/import/preview`. With `candidates`, no skill was chosen yet. */
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

export interface ImportApplyResult {
  skill: MySkill;
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

export interface MySkillListResponse {
  skills: MySkill[];
  totalCount: number;
}

export interface MySkillResourcesResponse {
  skillId: string;
  resources: MySkillResourceRef[];
}

/**
 * Request body for POST /skills/mine. No `skillId` — the backend allocates one
 * from the display name, so two users can both name a skill "Docx".
 */
export interface CreateMySkillRequest {
  displayName: string;
  description: string;
  instructions?: string;
  allowedTools?: string[];
  skillMetadata?: Record<string, unknown>;
  category?: string | null;
}

/**
 * Request body for PUT /skills/mine/{id}. All fields optional (partial update).
 */
export interface UpdateMySkillRequest {
  displayName?: string;
  description?: string;
  instructions?: string;
  allowedTools?: string[];
  skillMetadata?: Record<string, unknown>;
  category?: string | null;
  status?: SkillStatus;
  /**
   * Sent once the user confirmed the reach a 409 reported: the edit changes what
   * runs in shared agents or projects that use this skill unpinned.
   */
  confirmReach?: boolean;
}

/**
 * Bundle directory labels, used for the upload picker and file list grouping.
 */
export const RESOURCE_KINDS: { value: SkillResourceKind; label: string; hint: string }[] = [
  {
    value: 'reference',
    label: 'Reference',
    hint: 'Markdown or text the agent can read on demand.',
  },
  {
    value: 'script',
    label: 'Script',
    hint: 'Stored for reference — never executed on this platform.',
  },
  { value: 'asset', label: 'Asset', hint: 'Images, templates and other binaries.' },
];

/** Per-file upload ceiling, mirroring the backend `MAX_RESOURCE_BYTES`. */
export const MAX_RESOURCE_BYTES = 1_048_576;

/** Per-skill file ceiling, mirroring the backend `MAX_RESOURCES_PER_SKILL`. */
export const MAX_RESOURCES_PER_SKILL = 50;
