import { Injectable, inject, resource, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../services/config.service';
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
  private config = inject(ConfigService);
  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/memory`);

  /**
   * Reactive resource for fetching memory status
   */
  readonly memoryStatus = resource({
    loader: async () => {
      await Promise.resolve();
      return this.fetchMemoryStatus();
    }
  });

  /**
   * Reactive resource for fetching all user memories
   */
  readonly allMemories = resource({
    loader: async () => {
      await Promise.resolve();
      return this.fetchAllMemories();
    }
  });

  /**
   * Fetch memory status
   */
  async fetchMemoryStatus(): Promise<MemoryStatus> {
    const response = await firstValueFrom(
      this.http.get<MemoryStatus>(`${this.baseUrl()}/status`)
    );
    return response;
  }

  /**
   * Fetch all user memories (preferences and facts)
   */
  async fetchAllMemories(topK: number = 20): Promise<AllMemoriesResponse> {
    const response = await firstValueFrom(
      this.http.get<AllMemoriesResponse>(`${this.baseUrl()}?topK=${topK}`)
    );
    return response;
  }

  /**
   * Fetch user preferences
   */
  async fetchPreferences(query?: string, topK: number = 10): Promise<MemoriesResponse> {
    let url = `${this.baseUrl()}/preferences?topK=${topK}`;
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
    let url = `${this.baseUrl()}/facts?topK=${topK}`;
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
      this.http.post<MemoriesResponse>(`${this.baseUrl()}/search`, request)
    );
    return response;
  }

  /**
   * Fetch memory strategies
   */
  async fetchStrategies(): Promise<StrategiesResponse> {
    const response = await firstValueFrom(
      this.http.get<StrategiesResponse>(`${this.baseUrl()}/strategies`)
    );
    return response;
  }

  /**
   * Delete a memory record by ID
   */
  async deleteMemory(recordId: string): Promise<DeleteMemoryResponse> {
    const response = await firstValueFrom(
      this.http.delete<DeleteMemoryResponse>(`${this.baseUrl()}/${recordId}`)
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
