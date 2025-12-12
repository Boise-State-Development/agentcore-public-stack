import { Injectable, signal, computed } from '@angular/core';

export interface Model {
  name: string;
  modelId: string;
  provider: string;
  providerName: string;
}

@Injectable({
  providedIn: 'root'
})
export class ModelService {
  // Hardcoded models (will be fetched from API later)
  private readonly models = signal<Model[]>([
    {
      name: 'Claude Haiku 4.5',
      modelId: 'us.anthropic.claude-haiku-4-5-20251001-v1:0',
      provider: 'bedrock',
      providerName: 'Anthropic'
    },
    {
      name: 'OpenAI GPT 5.2',
      modelId: 'gpt-5.2',
      provider: 'openai',
      providerName: 'OpenAI'
    },
    {
      name: 'Gemini 3',
      modelId: 'gemini-3-pro-preview',
      provider: 'gemini',
      providerName: 'Google'
    }
  ]);

  // Selected model (defaults to first model)
  private readonly _selectedModel = signal<Model>(this.models()[0]);

  // Public read-only signals
  readonly availableModels = this.models.asReadonly();
  readonly selectedModel = this._selectedModel.asReadonly();

  /**
   * Sets the selected model
   */
  setSelectedModel(model: Model): void {
    this._selectedModel.set(model);
  }

  /**
   * Gets the currently selected model (for non-signal contexts)
   */
  getSelectedModel(): Model {
    return this._selectedModel();
  }
}
