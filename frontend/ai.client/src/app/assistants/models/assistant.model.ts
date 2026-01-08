export interface Assistant {
  assistantId: string;
  ownerId: string;
  name: string;
  description: string;
  instructions: string;
  vectorIndexId: string;
  visibility: 'PRIVATE' | 'PUBLIC' | 'SHARED';
  tags: string[];
  usageCount: number;
  createdAt: string;
  updatedAt: string;
  status: 'DRAFT' | 'COMPLETE' | 'ARCHIVED';
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
}

export interface UpdateAssistantRequest {
  name?: string;
  description?: string;
  instructions?: string;
  vectorIndexId?: string;
  visibility?: 'PRIVATE' | 'PUBLIC' | 'SHARED';
  tags?: string[];
  status?: 'DRAFT' | 'COMPLETE' | 'ARCHIVED';
}

export interface AssistantsListResponse {
  assistants: Assistant[];
  nextToken?: string;
}

