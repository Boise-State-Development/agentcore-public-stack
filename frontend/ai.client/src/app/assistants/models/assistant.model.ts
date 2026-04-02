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

  // Share metadata (only present for shared assistants)
  firstInteracted?: boolean;
  isSharedWithMe?: boolean;
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
}

export interface UnshareAssistantRequest {
  emails: string[];
}

export interface AssistantSharesResponse {
  assistantId: string;
  sharedWith: string[];
}

export interface UserSearchResult {
  userId: string;
  email: string;
  name: string;
}

export interface UserSearchResponse {
  users: UserSearchResult[];
}
