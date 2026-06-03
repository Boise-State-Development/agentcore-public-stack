export interface SystemPromptAdmin {
  prompt_id: string;
  name: string;
  description: string;
  prompt_text: string;
  status: 'enabled' | 'disabled';
  created_at: string;
  updated_at: string;
  created_by?: string | null;
}

export interface SystemPromptsAdminListResponse {
  prompts: SystemPromptAdmin[];
  total: number;
}

export interface SystemPromptFormData {
  name: string;
  description: string;
  prompt_text: string;
  status: 'enabled' | 'disabled';
}
