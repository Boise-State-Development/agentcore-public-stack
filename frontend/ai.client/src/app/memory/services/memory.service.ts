import { Injectable, inject, resource } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../environments/environment';
import { AuthService } from '../../auth/auth.service';
import {
  MemoryStatus,
  MemoriesResponse,
  AllMemoriesResponse,
  StrategiesResponse,
  MemorySearchRequest,
  DeleteMemoryResponse,
} from '../models/memory.model';

/**
 * Service for accessing AgentCore Memory data
 *
 * Provides methods for retrieving user preferences, facts, and performing
 * semantic search across memories.
 */
@Injectable({
  providedIn: 'root'
})
export class MemoryService {
  private http = inject(HttpClient);
  private authService = inject(AuthService);

  /**
   * Reactive resource for fetching memory status
   */
  readonly memoryStatus = resource({
    loader: async () => {
      await this.authService.ensureAuthenticated();
      return this.fetchMemoryStatus();
    }
  });

  /**
   * Reactive resource for fetching all user memories
   */
  readonly allMemories = resource({
    loader: async () => {
      await this.authService.ensureAuthenticated();
      return this.fetchAllMemories();
    }
  });

  /**
   * Fetch memory status
   */
  async fetchMemoryStatus(): Promise<MemoryStatus> {
    const response = await firstValueFrom(
      this.http.get<MemoryStatus>(`${environment.appApiUrl}/memory/status`)
    );
    return response;
  }

  /**
   * Fetch all user memories (preferences and facts)
   */
  async fetchAllMemories(topK: number = 20): Promise<AllMemoriesResponse> {
    const response = await firstValueFrom(
      this.http.get<AllMemoriesResponse>(`${environment.appApiUrl}/memory?topK=${topK}`)
    );
    return response;
  }

  /**
   * Fetch user preferences
   */
  async fetchPreferences(query?: string, topK: number = 10): Promise<MemoriesResponse> {
    let url = `${environment.appApiUrl}/memory/preferences?topK=${topK}`;
    if (query) {
      url += `&query=${encodeURIComponent(query)}`;
    }
    const response = await firstValueFrom(
      this.http.get<MemoriesResponse>(url)
    );
    return response;
  }

  /**
   * Fetch user facts
   */
  async fetchFacts(query?: string, topK: number = 10): Promise<MemoriesResponse> {
    let url = `${environment.appApiUrl}/memory/facts?topK=${topK}`;
    if (query) {
      url += `&query=${encodeURIComponent(query)}`;
    }
    const response = await firstValueFrom(
      this.http.get<MemoriesResponse>(url)
    );
    return response;
  }

  /**
   * Semantic search across memories
   */
  async searchMemories(request: MemorySearchRequest): Promise<MemoriesResponse> {
    const response = await firstValueFrom(
      this.http.post<MemoriesResponse>(`${environment.appApiUrl}/memory/search`, request)
    );
    return response;
  }

  /**
   * Fetch memory strategies
   */
  async fetchStrategies(): Promise<StrategiesResponse> {
    const response = await firstValueFrom(
      this.http.get<StrategiesResponse>(`${environment.appApiUrl}/memory/strategies`)
    );
    return response;
  }

  /**
   * Delete a memory record by ID
   */
  async deleteMemory(recordId: string): Promise<DeleteMemoryResponse> {
    const response = await firstValueFrom(
      this.http.delete<DeleteMemoryResponse>(`${environment.appApiUrl}/memory/${recordId}`)
    );
    return response;
  }

  /**
   * Reload all memory resources
   */
  reload(): void {
    this.memoryStatus.reload();
    this.allMemories.reload();
  }
}
