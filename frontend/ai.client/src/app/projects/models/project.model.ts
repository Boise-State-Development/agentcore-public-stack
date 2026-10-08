/**
 * Shared Projects wire types (docs/specs/shared-projects.md §5).
 *
 * Mirrors `backend/src/apis/app_api/projects/models.py`. User ids never cross the
 * wire: people are identified by email, and `hasSignedIn` says whether one has
 * signed in yet (ownership transfer needs it). Beside each email is the person's
 * display name, or null when the directory has none; show it with `personLabel`.
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
  ownerName: string | null;
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
  name: string | null;
  role: ProjectRole;
  /**
   * Signed in to the platform, so they can be made owner. Not whether they have
   * opened this project.
   */
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
  createdByName: string | null;
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
  sharedByName: string | null;
  sharedAt: string;
  /** The existing `/shared/{shareId}` view. */
  shareUrl: string;
  /** The caller shared it, so the caller may revoke it. */
  isMine: boolean;
  /** The sharer's note (2.5b), when they left one. */
  note?: string | null;
}

/** An artifact a member shared with the project (3.3), as `/projects/{id}/outputs` returns it. */
export interface ProjectOutput {
  artifactId: string;
  shareId: string;
  version: number;
  title: string;
  contentType: string;
  sharedByEmail: string;
  sharedByName?: string | null;
  sharedAt: string;
  /** The shared-artifact view, `/shared-artifact/{shareId}`. */
  shareUrl: string;
  isMine: boolean;
  /** The sharer, or an editor of an active project. */
  canRemove: boolean;
}

export interface ProjectOutputsResponse {
  /** Most recently shared first. */
  outputs: ProjectOutput[];
}

/** One item a maintenance change reads, with its text when the change was planned. */
export interface MaintenanceOpSource {
  anchor: string;
  text: string;
}

/**
 * One change a maintenance run proposes (2.6a). `merge`: `sources` become one item, `text`,
 * kept under `keep`'s anchor. `supersede`: `sources` is `[old, new]`, and the old one leaves.
 * `prune`: the one source leaves because every date in it has passed. `split` (2.6c): `sources`
 * move, unchanged, to a new file `newSlug` described by `description`, and `text` is the
 * pointer item that takes their place.
 */
export interface MaintenanceOp {
  type: 'merge' | 'supersede' | 'prune' | 'split';
  sources: MaintenanceOpSource[];
  text?: string | null;
  keep?: string | null;
  reason?: 'expired' | null;
  /** The planner's reason, for the reviewer. */
  why: string;
  newSlug?: string | null;
  description?: string | null;
}

/** A proposed change to the project's shared memory (2.5a), as `/memory/proposals` returns it. */
export interface MemoryProposal {
  proposalId: string;
  /** `compaction`: a maintenance run's changes (2.6a), reviewed op by op. Absent before 2.6a. */
  kind?: 'entry' | 'compaction';
  state: 'pending' | 'approved' | 'rejected' | 'withdrawn';
  /** The memory file it creates or replaces. */
  slug: string;
  /** The whole proposed file, one "- " item per line. */
  text: string;
  description?: string | null;
  /** 0 for a new file. */
  baseVersion: number;
  proposedByEmail: string;
  proposedByName?: string | null;
  proposerKind: 'member' | 'agent' | 'schedule' | 'maintenance';
  createdAt: string;
  decidedByEmail?: string | null;
  decidedByName?: string | null;
  decidedAt?: string | null;
  note?: string | null;
  resultVersion?: number | null;
  edited: boolean;
  isMine: boolean;
  /**
   * Pending, and the file changed since: approve an edited version or decline. For a
   * maintenance proposal, none of its changes still apply.
   */
  stale?: boolean | null;
  /** Maintenance proposals: the changes, the run that planned them, and which were applied. */
  ops?: MaintenanceOp[] | null;
  runId?: string | null;
  appliedOps?: number[] | null;
  /** The files an approved split created (2.6c). */
  createdFiles?: string[] | null;
}

export interface MemoryProposalDetail extends MemoryProposal {
  /** Pending only: the file now, items without frontmatter; null for a new file. */
  currentText?: string | null;
}

export interface MemoryProposalsResponse {
  proposals: MemoryProposal[];
}

// ---- the Memory tab (2.8) ----------------------------------------------

/** `project` is the shared memory every member's tasks load; `mine` is the caller's own in this project. */
export type MemoryScope = 'project' | 'mine';

/** The size limits the Memory tab's meters measure against. */
export interface MemoryLimits {
  /** A file over this can't be saved. */
  fileHardCapTokens: number;
  /** A file at this is close to the cap. */
  fileSoftThresholdTokens: number;
  /** How much of each scope's MEMORY.md reaches a task (estimated at four characters a token). */
  projectIndexBudgetTokens: number;
  personalIndexBudgetTokens: number;
}

/** `GET /projects/{id}/memory`: the project's spaces as the caller sees them. */
export interface ProjectMemory {
  /** Null while Memory Spaces are off, or for an archived project made before 2.4. */
  sharedSpaceId: string | null;
  /** Null until the caller keeps something of their own here. */
  personalSpaceId: string | null;
  role: ProjectRole;
  limits?: MemoryLimits | null;
}

/** One memory file as the space's manifest lists it (`GET /memory/spaces/{id}/entries`). */
export interface MemoryEntry {
  slug: string;
  description: string;
  updated: string;
  updatedBy: string;
  updatedByName: string | null;
  aliases: string[];
  tokens: number | null;
  tokensMethod: string | null;
  itemCount: number | null;
  archived: boolean;
  version: number;
}

export interface MemoryEntriesResponse {
  entries: MemoryEntry[];
}

/** Where one item came from. People are emails; `MemoryFile.people` names them. */
export interface ItemProvenance {
  addedBy: string;
  addedAt: string;
  updatedBy?: string | null;
  updatedAt?: string | null;
  /** The task the latest change came from, when a task's assistant wrote it. */
  sourceSessionId?: string | null;
  proposalId?: string | null;
  proposedBy?: string | null;
  approvedBy?: string | null;
  restoredBy?: string | null;
  restoredAt?: string | null;
  /** A tidy-up's split moved it here from this file (2.6c). */
  movedFrom?: string | null;
  movedBy?: string | null;
  movedAt?: string | null;
}

/** An item this one replaced, still in the archive: its supersede marker (2.6c). */
export interface ReplacedMemoryItem {
  archiveId: string;
  text: string;
  /** `merged`: folded into this item. `superseded`: this newer item replaced it. */
  reason: 'merged' | 'superseded';
  archivedAt: string;
  restorableUntil: string;
}

export interface MemoryItem {
  anchor: string;
  text: string;
  pinned: boolean;
  /** Null for an item saved before provenance was recorded and not changed since. */
  provenance?: ItemProvenance | null;
  /** What it replaced, oldest first; absent or empty when nothing. */
  replaces?: ReplacedMemoryItem[];
}

/** `GET /projects/{id}/memory/files/{slug}`: one file as items. */
export interface MemoryFile {
  slug: string;
  description: string;
  version: number;
  tokens: number | null;
  items: MemoryItem[];
  /** Display names by email, for the people in `provenance`. */
  people: Record<string, string>;
}

export interface MemoryPinsResponse {
  slug: string;
  pinned: string[];
}

export interface ArchivedMemoryItem {
  archiveId: string;
  slug: string;
  anchor: string;
  text: string;
  /** `removed`: a save left it out. `deleted`: its whole file was deleted. */
  /** `merged`, `superseded`, `pruned`: an approved maintenance change took it out (2.6a). */
  reason: 'removed' | 'deleted' | 'merged' | 'superseded' | 'pruned';
  /** The anchor of the item that took its place (a merge or a supersede). */
  supersededBy?: string | null;
  archivedBy: string;
  archivedAt: string;
  restorableUntil: string;
  provenance?: ItemProvenance | null;
}

export interface MemoryArchiveResponse {
  /** Newest first; only those still restorable. */
  items: ArchivedMemoryItem[];
  people: Record<string, string>;
}

export interface MemoryRestoreResponse {
  slug: string;
  version: number;
}

/** One saved version of a memory file (`GET /memory/spaces/{id}/history?slug=`). */
export interface MemoryFileVersion {
  version: number;
  contentHash: string;
  size: number;
  tokens: number | null;
  /** Email of who saved it; empty when unknown. */
  updatedBy: string;
  updatedByName: string | null;
  updatedAt: string;
  /** `edit` (in the Memory tab), `save` (by the assistant), `proposal`, `restore`, `baseline` or `maintenance`. */
  reason: string;
}

export interface MemoryFileHistory {
  slug: string;
  /** Newest first. */
  versions: MemoryFileVersion[];
}

export interface MemoryFileVersionContent extends MemoryFileVersion {
  slug: string;
  /** The file as saved then, frontmatter included. */
  content: string;
}

/** One item as the editor sends it: the anchor it was read with, or none for a new item. */
export interface EditedMemoryItem {
  anchor?: string | null;
  text: string;
}

/** `PUT /projects/{id}/memory/files/{slug}`: a file from its items. */
export interface SaveMemoryFileRequest {
  items: EditedMemoryItem[];
  /** Omit to keep the current one. */
  description?: string;
  aliases?: string[];
  /** The version opened, 0 for a new file; a file that moved on since is a 409. */
  baseVersion?: number;
}

export interface SaveMemoryFileResponse {
  slug: string;
  version: number;
  tokens: number | null;
  itemCount: number | null;
  warnings: string[];
  overSoftThreshold: boolean;
  removedAnchors: string[];
  /** A new file's index line: `added`, `already_linked` or `over_budget`; null for an existing file. */
  indexed: string | null;
}

/** `POST /projects/{id}/memory/proposals`: a whole file, in the form a save takes. */
export interface CreateMemoryProposalRequest {
  slug: string;
  text: string;
  description?: string;
  aliases?: string[];
}

export interface SharedTasksResponse {
  /** Most recently shared first; one entry per task. */
  tasks: SharedTask[];
}

// ---- files (the project agent's documents) -----------------------------

export interface ProjectDocument extends Document {
  /** Null when unknown: added before this was recorded, or by a former member. */
  addedByEmail: string | null;
  addedByName: string | null;
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

// ---- activity (the project's audit trail) ------------------------------

/** One `project.*` audit record as editors see it: by email, never by user id. */
export interface ProjectAuditRecord {
  auditId: string;
  timestamp: string;
  /** `project.created`, `project.member_added`, … */
  action: string;
  actorEmail?: string | null;
  changes?: string[] | null;
  before?: Record<string, unknown> | null;
  after?: Record<string, unknown> | null;
  reason?: string | null;
}

export interface ProjectAuditResponse {
  /** Newest first. */
  records: ProjectAuditRecord[];
  /** Display names by email for the people the page mentions (actors and members). */
  people?: Record<string, string>;
  nextCursor?: string | null;
}

// ---- maintenance (2.6a, 2.6b) -------------------------------------------

export type MaintenanceRunState = 'queued' | 'running' | 'done' | 'failed';

/**
 * What a run did with one file. `proposed`: changes wait for review (project memory).
 * `applied`: the changes were saved (your own memory, 2.6b). `changed`: the file was saved
 * while the run planned it, so it was left alone. `created`: a new file a split made from
 * `splitFrom` (2.6c).
 */
export interface MaintenanceFileResult {
  slug: string;
  outcome: 'proposed' | 'applied' | 'changed' | 'nothing_to_do' | 'pending_review' | 'failed' | 'not_reached' | 'created';
  proposalId?: string | null;
  planned: number;
  kept: number;
  dropped: number;
  error?: string | null;
  /** Applied: the version the run saved, and the changes it made. */
  version?: number | null;
  ops?: MaintenanceOp[] | null;
  /** Applied, in a run too large to keep every change: counts only. */
  opsOmitted?: boolean;
  /**
   * After an undo: `restored` (put back, as `undoVersion`), `changed` (saved since the run, so
   * left alone), `missing` (deleted since), `failed`, or for a file a split made, `removed`.
   */
  undo?: 'restored' | 'changed' | 'missing' | 'failed' | 'removed' | null;
  undoVersion?: number | null;
  /** `created`: the file its items came from. */
  splitFrom?: string | null;
}

/**
 * A maintenance run. `scope` is the Memory page's: `project` (owner and editors see it, and it
 * ends in proposals) or `mine` (your own memory, saved straight away and undoable until
 * `undoableUntil`).
 */
export interface MaintenanceRun {
  runId: string;
  state: MaintenanceRunState;
  /** Absent before 2.6b, when every run was on project memory. */
  scope?: MemoryScope;
  /** The one file it maintains; null for every file. */
  slug?: string | null;
  requestedByEmail: string;
  requestedByName?: string | null;
  createdAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  results: MaintenanceFileResult[];
  error?: string | null;
  undoneAt?: string | null;
  /** Set while an applied run on your own memory can still be undone. */
  undoableUntil?: string | null;
}

export interface MaintenanceRunsResponse {
  /** Newest first. */
  runs: MaintenanceRun[];
}
