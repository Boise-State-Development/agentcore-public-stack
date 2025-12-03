// Updated conversation.model.ts

// Text Content Block
export interface TextContentBlock {
    text: string;
  }
  
  // Tool Use Content Block
  export interface ToolUseContentBlock {
    toolUse: {
      toolUseId: string;
      name: string;
      input: any;
      requiresApproval: boolean;
      approvalStatus: 'pending' | 'approved' | 'denied' | 'not_required';
    };
  }
  
  // Tool Result Text Content Block
  export interface ToolResultTextContent {
    text: string;
  }
  
  // Tool Result JSON Content Block
  export interface ToolResultJsonContent {
    json: any;
  }
  
  // Union type for tool result content blocks
  export type ToolResultContent = 
    | ToolResultTextContent
    | ToolResultJsonContent;
  
  // Tool Result Content Block
  export interface ToolResultBlock {
    toolUseId: string;
    toolName: string;
    content: ToolResultContent[];
    status: 'success' | 'error';
  }
  
  // Tool Result Content Block wrapper
  export interface ToolResultContentBlock {
    toolResult: ToolResultBlock;
  }
  
  // Legacy ToolResult interface (to be deprecated)
  export interface ToolResult {
    toolUseId: string;
    name: string;
    input: any;
    result: string;
    status: 'success' | 'error';
  }

  export interface FileReferenceContentBlock {
    fileReference: {
      fileName: string;
      fileSize: number;
      s3Key: string;
      format: string;
      type: 'image' | 'video' | 'document';
      contentType: string;
    };
  }
  
  // Union type for all content block types
  export type ContentBlock = 
    | TextContentBlock
    | FileReferenceContentBlock
    | ToolUseContentBlock
    | ToolResultContentBlock;
  
  // Updated Message interface
  export interface Message {
    id?: string;
    role: 'user' | 'assistant';
    content: ContentBlock[];
    createdAt?: string;
    reasoning?: string;
    usage?: {
      inputTokens: number;
      outputTokens: number;
    };
  }
  
  export interface Conversation {
    conversationId: string;
    name: string;
    createdAt?: string;
    isStarred?: boolean;
    assistantId?: string;
  assistantInstanceId?: string;
    instructionId?: string;
  }
  
  export interface Model {
    modelId: string;
    enabled: boolean;
    updatedAt: string;
    createdAt: string;
    description: string;
    outputPricePerMillionTokens: number;
    inputPricePerMillionTokens: number;
    name: string;
    sortOrder: number;
    isDefault: boolean;
    categories: string[];
    releaseDate?: string;
    modalities: string[];
    maxOutputTokens?: number;
    maxContextTokens?: number;
    languageText?: string;
    allowedRoles?: string[];
    creator?: string;
    effectiveDate?: string;
    isToolsCapable?: boolean;
  }
  
  export type Role = 'assistant' | 'user' | 'system';



