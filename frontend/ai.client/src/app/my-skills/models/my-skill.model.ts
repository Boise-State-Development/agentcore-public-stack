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

import { SkillStatus } from '../../admin/skills/models/admin-skill.model';

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
}

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
