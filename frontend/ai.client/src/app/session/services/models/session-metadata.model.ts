// Session Metadata Models
// Matches backend SessionMetadata and SessionPreferences models

export interface SessionPreferences {
  lastModel?: string;
  lastTemperature?: number;
  enabledTools?: string[];
  selectedPromptId?: string;
  customPromptText?: string;
}

export interface SessionMetadata {
  sessionId: string;
  userId: string;
  title: string;
  status: 'active' | 'archived' | 'deleted';
  createdAt: string;  // ISO 8601 timestamp
  lastMessageAt: string;  // ISO 8601 timestamp
  messageCount: number;
  starred?: boolean;
  tags?: string[];
  preferences?: SessionPreferences;
}

// Request model for updating session metadata
export interface UpdateSessionMetadataRequest {
  title?: string;
  status?: 'active' | 'archived' | 'deleted';
  starred?: boolean;
  tags?: string[];
  lastModel?: string;
  lastTemperature?: number;
  enabledTools?: string[];
  selectedPromptId?: string;
  customPromptText?: string;
}
