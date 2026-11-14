export type SharePermission = 'viewer' | 'editor';
export type UserPermission = 'owner' | SharePermission;

export interface Assistant {
  assistantId: string;
  ownerId: string;
  ownerName: string;
  name: string;
  description: string;
  instructions: string;
  vectorIndexId: string;
  visibility: 'PRIVATE' | 'PUBLIC' | 'SHARED';
  tags: string[];
  starters: string[];
  emoji?: string;
  usageCount: number;
  createdAt: string;
  updatedAt: string;
  status: 'DRAFT' | 'COMPLETE';
  imageUrl?: string;

  /** Configurable source citations & document download (#111); default true. */
  showCitations?: boolean;
  allowDocumentDownload?: boolean;

  /** How strictly answers stay within the knowledge base (#112); default 'balanced'. */
  responseStrictness?: 'strict' | 'balanced' | 'flexible';

  /** Whether people can talk to this assistant in voice mode (#625); default off. */
  voiceEnabled?: boolean;

  // Share metadata (only present for shared assistants)
  firstInteracted?: boolean;
  isSharedWithMe?: boolean;
  userPermission?: UserPermission;

  /**
   * Shared Projects: `'project'` when this is a project's hidden harness, which a
   * project task binds. The chat renders it as the project, not as an Agent.
   */
  kind?: 'project';
  /** The owning project when `kind === 'project'`. */
  projectId?: string;
}

export interface CreateAssistantDraftRequest {
  name?: string;
}

export interface CreateAssistantRequest {
  name: string;
  description: string;
  instructions: string;
  vectorIndexId: string;
  visibility?: 'PRIVATE' | 'PUBLIC' | 'SHARED';
  tags?: string[];
  starters?: string[];
  emoji?: string;
}

export interface UpdateAssistantRequest {
  name?: string;
  description?: string;
  instructions?: string;
  vectorIndexId?: string;
  visibility?: 'PRIVATE' | 'PUBLIC' | 'SHARED';
  tags?: string[];
  starters?: string[];
  emoji?: string;
  status?: 'DRAFT' | 'COMPLETE';
}

export interface AssistantsListResponse {
  assistants: Assistant[];
  nextToken?: string;
}

export interface ShareAssistantRequest {
  emails: string[];
  permission?: SharePermission;
}

export interface UnshareAssistantRequest {
  emails: string[];
}

export interface UpdateSharePermissionRequest {
  email: string;
  permission: SharePermission;
}

export interface ShareEntry {
  email: string;
  permission: SharePermission;
}

export interface AssistantSharesResponse {
  assistantId: string;
  sharedWith: ShareEntry[];
}

export interface UserSearchResult {
  userId: string;
  email: string;
  name: string;
}

export interface UserSearchResponse {
  users: UserSearchResult[];
}
