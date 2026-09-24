/**
 * Shared Projects wire types (docs/specs/shared-projects.md §5).
 *
 * Mirrors `backend/src/apis/app_api/projects/models.py`. User ids never cross the
 * wire: people are identified by email, and `hasSignedIn` says whether one has
 * signed in yet (ownership transfer needs it).
 */

import type { Document, KbUsage, UploadUrlResponse } from '../../assistants/models/document.model';
import type { SessionMetadata } from '../../session/services/models/session-metadata.model';

export type ProjectRole = 'owner' | 'editor' | 'viewer';
export type MemberRole = 'editor' | 'viewer';
export type ProjectStatus = 'active' | 'archived';

export interface Project {
  projectId: string;
  name: string;
  description: string;
  ownerEmail: string;
  /** The caller's role on this project. */
  role: ProjectRole;
  status: ProjectStatus;
  editorsManageMembers: boolean;
  /** Members besides the owner. */
  memberCount: number;
  harnessAgentId: string;
  createdAt: string;
  updatedAt: string;
}

export interface ProjectListResponse {
  projects: Project[];
}

export interface CreateProjectRequest {
  name: string;
  description?: string;
}

export interface UpdateProjectRequest {
  name?: string;
  description?: string;
  editorsManageMembers?: boolean;
  status?: ProjectStatus;
}

export interface ProjectMember {
  email: string;
  role: ProjectRole;
  hasSignedIn: boolean;
  createdAt?: string;
}

export interface MembersResponse {
  /** Owner first, then members by email. */
  members: ProjectMember[];
  /** Whether the caller may add, change or remove people. */
  canManage: boolean;
}

export interface AddMembersResponse {
  added: ProjectMember[];
  alreadyMembers: string[];
  invalid: string[];
  overCapacity: string[];
}

export interface DirectoryPerson {
  email: string;
  name: string;
  hasSignedIn: boolean;
  /** Set when the person is already in the project. */
  memberRole: ProjectRole | null;
}

export interface DirectoryResponse {
  people: DirectoryPerson[];
}

// ---- settings (the project's agent) ------------------------------------

export interface ModelConfig {
  modelId: string;
  provider?: string | null;
  params?: Record<string, unknown> | null;
}

export interface BindingRef {
  ref: string;
  config?: Record<string, unknown>;
}

interface SettingsResponse {
  /** Current version, or null before the first save. */
  version: number | null;
  canEdit: boolean;
}

export interface InstructionsResponse extends SettingsResponse {
  instructions: string;
}

export interface ModelResponse extends SettingsResponse {
  /** Null: each member's own default model. */
  modelConfig: ModelConfig | null;
}

export interface BindingsResponse extends SettingsResponse {
  bindings: BindingRef[];
}

export interface SettingsVersionSummary {
  version: number;
  createdAt: string | null;
  /** Null for the state the project was created with. */
  createdByEmail: string | null;
  /** Fields changed from the previous version (`instructions`, `bindings`, `modelConfig`, …). */
  changes: string[];
}

export interface SettingsVersionsResponse {
  versions: SettingsVersionSummary[];
}

export interface VersionFieldChange {
  field: string;
  before?: unknown;
  after?: unknown;
  behavior: boolean;
}

export interface SettingsVersion extends SettingsVersionSummary {
  instructions: string;
  modelConfig: ModelConfig | null;
  tools: BindingRef[];
  skills: BindingRef[];
  fieldChanges: VersionFieldChange[];
  instructionsDiff: string[];
}

// ---- tasks --------------------------------------------------------------

/** `GET /projects/{id}/tasks`: the caller's own tasks, the `/sessions` list shape. */
export interface ProjectTasksResponse {
  /** Newest first. */
  sessions: SessionMetadata[];
  /** Value cursor for the next page; null on the last one. */
  nextToken: string | null;
}

/** A task a member shared with the project (a `SHARED_TASK#` pointer). */
export interface SharedTask {
  shareId: string;
  title: string;
  sharedByEmail: string;
  sharedAt: string;
  /** The existing `/shared/{shareId}` view. */
  shareUrl: string;
  /** The caller shared it, so the caller may revoke it. */
  isMine: boolean;
}

export interface SharedTasksResponse {
  /** Most recently shared first; one entry per task. */
  tasks: SharedTask[];
}

// ---- files (the project agent's documents) -----------------------------

export interface ProjectDocument extends Document {
  /** Null when unknown: added before this was recorded, or by a former member. */
  addedByEmail: string | null;
}

export interface ProjectDocumentsResponse {
  documents: ProjectDocument[];
  nextToken?: string | null;
  kbUsage?: KbUsage | null;
  /** Editor or owner on an active project. */
  canEdit: boolean;
}

export interface ProjectUploadUrlResponse extends UploadUrlResponse {
  /** "Everyone in {project} ({n} people) can open this file…" — shown at upload. */
  notice: string;
}
