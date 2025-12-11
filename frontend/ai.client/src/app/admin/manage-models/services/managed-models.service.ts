import { Injectable, signal, computed } from '@angular/core';
import { ManagedModel } from '../models/managed-model.model';

/**
 * Service to manage the list of models that have been added to the system.
 * This service maintains the state of managed models and provides utilities
 * to check if a model has already been added.
 */
@Injectable({
  providedIn: 'root'
})
export class ManagedModelsService {
  // Mock data for managed models (same as in manage-models.page.ts)
  // In a real app, this would be fetched from an API
  private managedModels = signal<ManagedModel[]>([
    {
      id: '1',
      modelId: 'anthropic.claude-3-5-sonnet-20241022-v2:0',
      modelName: 'Claude 3.5 Sonnet v2',
      providerName: 'Anthropic',
      inputModalities: ['TEXT', 'IMAGE'],
      outputModalities: ['TEXT'],
      responseStreamingSupported: true,
      customizationsSupported: [],
      inferenceTypesSupported: ['ON_DEMAND'],
      modelLifecycle: 'ACTIVE',
      availableToRoles: ['Admin', 'SuperAdmin', 'User'],
      enabled: true,
      inputPricePerMillionTokens: 3.0,
      outputPricePerMillionTokens: 15.0,
      createdAt: new Date('2024-10-22'),
      updatedAt: new Date('2024-12-01'),
    },
    {
      id: '2',
      modelId: 'anthropic.claude-3-haiku-20240307-v1:0',
      modelName: 'Claude 3 Haiku',
      providerName: 'Anthropic',
      inputModalities: ['TEXT', 'IMAGE'],
      outputModalities: ['TEXT'],
      responseStreamingSupported: true,
      customizationsSupported: [],
      inferenceTypesSupported: ['ON_DEMAND'],
      modelLifecycle: 'ACTIVE',
      availableToRoles: ['Admin', 'SuperAdmin', 'User', 'Guest'],
      enabled: true,
      inputPricePerMillionTokens: 0.25,
      outputPricePerMillionTokens: 1.25,
      createdAt: new Date('2024-03-07'),
      updatedAt: new Date('2024-11-15'),
    },
    {
      id: '3',
      modelId: 'amazon.titan-text-express-v1',
      modelName: 'Titan Text G1 - Express',
      providerName: 'Amazon',
      inputModalities: ['TEXT'],
      outputModalities: ['TEXT'],
      responseStreamingSupported: true,
      customizationsSupported: ['FINE_TUNING'],
      inferenceTypesSupported: ['ON_DEMAND', 'PROVISIONED'],
      modelLifecycle: 'ACTIVE',
      availableToRoles: ['Admin', 'SuperAdmin'],
      enabled: false,
      inputPricePerMillionTokens: 0.2,
      outputPricePerMillionTokens: 0.6,
      createdAt: new Date('2024-01-15'),
      updatedAt: new Date('2024-10-20'),
    },
    {
      id: '4',
      modelId: 'meta.llama3-70b-instruct-v1:0',
      modelName: 'Llama 3 70B Instruct',
      providerName: 'Meta',
      inputModalities: ['TEXT'],
      outputModalities: ['TEXT'],
      responseStreamingSupported: true,
      customizationsSupported: [],
      inferenceTypesSupported: ['ON_DEMAND'],
      modelLifecycle: 'ACTIVE',
      availableToRoles: ['Admin', 'User'],
      enabled: true,
      inputPricePerMillionTokens: 0.99,
      outputPricePerMillionTokens: 0.99,
      createdAt: new Date('2024-04-18'),
      updatedAt: new Date('2024-11-30'),
    },
  ]);

  // Computed set of model IDs for quick lookup
  private addedModelIds = computed(() => {
    return new Set(this.managedModels().map(m => m.modelId));
  });

  /**
   * Get all managed models
   */
  getManagedModels() {
    return this.managedModels();
  }

  /**
   * Check if a model with the given modelId has already been added
   */
  isModelAdded(modelId: string): boolean {
    return this.addedModelIds().has(modelId);
  }

  /**
   * Add a new managed model (for future implementation)
   */
  addModel(model: ManagedModel): void {
    this.managedModels.update(models => [...models, model]);
  }

  /**
   * Remove a managed model (for future implementation)
   */
  removeModel(modelId: string): void {
    this.managedModels.update(models => models.filter(m => m.id !== modelId));
  }

  /**
   * Update a managed model (for future implementation)
   */
  updateModel(modelId: string, updatedModel: Partial<ManagedModel>): void {
    this.managedModels.update(models =>
      models.map(m => m.id === modelId ? { ...m, ...updatedModel } : m)
    );
  }
}
