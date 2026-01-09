import { Injectable, signal, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { 
  Assistant, 
  CreateAssistantDraftRequest,
  CreateAssistantRequest, 
  UpdateAssistantRequest 
} from '../models/assistant.model';
import { AssistantApiService } from './assistant-api.service';

@Injectable({
  providedIn: 'root'
})
export class AssistantService {
  private apiService = inject(AssistantApiService);

  private assistants = signal<Assistant[]>([]);
  private loading = signal<boolean>(false);
  private error = signal<string | null>(null);

  readonly assistants$ = this.assistants.asReadonly();
  readonly loading$ = this.loading.asReadonly();
  readonly error$ = this.error.asReadonly();

  async createDraft(request: CreateAssistantDraftRequest = {}): Promise<Assistant> {
    this.loading.set(true);
    this.error.set(null);

    try {
      const assistant = await firstValueFrom(this.apiService.createDraft(request));
      if (!assistant) {
        throw new Error('No assistant returned from API');
      }
      return assistant;
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to create draft assistant';
      this.error.set(errorMessage);
      throw err;
    } finally {
      this.loading.set(false);
    }
  }

  async loadAssistants(includeDrafts = false, includeArchived = false, includePublic = false): Promise<void> {
    this.loading.set(true);
    this.error.set(null);

    try {
      const response = await firstValueFrom(this.apiService.getAssistants({
        includeDrafts,
        includeArchived,
        includePublic
      }));

      if (response) {
        this.assistants.set(response.assistants);
      }
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to load assistants';
      this.error.set(errorMessage);
      throw err;
    } finally {
      this.loading.set(false);
    }
  }

  async createAssistant(request: CreateAssistantRequest): Promise<Assistant> {
    this.loading.set(true);
    this.error.set(null);

    try {
      const assistant = await firstValueFrom(this.apiService.createAssistant(request));
      if (!assistant) {
        throw new Error('No assistant returned from API');
      }

      // Add to local list
      this.assistants.update(current => [...current, assistant]);
      return assistant;
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to create assistant';
      this.error.set(errorMessage);
      throw err;
    } finally {
      this.loading.set(false);
    }
  }

  async updateAssistant(id: string, request: UpdateAssistantRequest): Promise<Assistant> {
    this.loading.set(true);
    this.error.set(null);

    try {
      const updatedAssistant = await firstValueFrom(this.apiService.updateAssistant(id, request));
      if (!updatedAssistant) {
        throw new Error('No assistant returned from API');
      }

      // Update in local list
      this.assistants.update(current =>
        current.map(a => a.assistantId === id ? updatedAssistant : a)
      );

      return updatedAssistant;
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to update assistant';
      this.error.set(errorMessage);
      throw err;
    } finally {
      this.loading.set(false);
    }
  }

  async archiveAssistant(id: string): Promise<void> {
    this.loading.set(true);
    this.error.set(null);

    try {
      await firstValueFrom(this.apiService.archiveAssistant(id));

      // Remove from local list (archived assistants are hidden by default)
      this.assistants.update(current =>
        current.filter(a => a.assistantId !== id)
      );
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to archive assistant';
      this.error.set(errorMessage);
      throw err;
    } finally {
      this.loading.set(false);
    }
  }

  async deleteAssistant(id: string): Promise<void> {
    this.loading.set(true);
    this.error.set(null);

    try {
      await firstValueFrom(this.apiService.deleteAssistant(id));

      // Remove from local list
      this.assistants.update(current =>
        current.filter(a => a.assistantId !== id)
      );
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to delete assistant';
      this.error.set(errorMessage);
      throw err;
    } finally {
      this.loading.set(false);
    }
  }

  async getAssistant(id: string): Promise<Assistant> {
    this.loading.set(true);
    this.error.set(null);

    try {
      const assistant = await firstValueFrom(this.apiService.getAssistant(id));
      if (!assistant) {
        throw new Error('Assistant not found');
      }

      // Update local cache
      const exists = this.assistants().some(a => a.assistantId === id);
      if (exists) {
        this.assistants.update(current =>
          current.map(a => a.assistantId === id ? assistant : a)
        );
      } else {
        this.assistants.update(current => [...current, assistant]);
      }

      return assistant;
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to get assistant';
      this.error.set(errorMessage);
      throw err;
    } finally {
      this.loading.set(false);
    }
  }

  getAssistantById(id: string): Assistant | undefined {
    return this.assistants().find(assistant => assistant.assistantId === id);
  }
}
