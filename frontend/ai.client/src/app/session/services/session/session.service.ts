import { inject, Injectable, signal, WritableSignal, resource, computed } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../../environments/environment';
import { AuthService } from '../../../auth/auth.service';
import { SessionMetadata, UpdateSessionMetadataRequest } from '../models/session-metadata.model';
import { Message } from '../models/message.model';

/**
 * Query parameters for listing sessions.
 */
export interface ListSessionsParams {
  /** Maximum number of sessions to return (optional, no limit if not specified, max: 1000) */
  limit?: number;
  /** Pagination token for retrieving the next page of results */
  next_token?: string | null;
}

/**
 * Response model for listing sessions with pagination support.
 * 
 * Matches the SessionsListResponse model from the Python API.
 */
export interface SessionsListResponse {
  /** List of sessions for the user */
  sessions: SessionMetadata[];
  /** Pagination token for retrieving the next page of results */
  next_token: string | null;
}

/**
 * Response model for listing messages with pagination support.
 * 
 * Matches the MessagesListResponse model from the Python API.
 */
export interface MessagesListResponse {
  /** List of messages in the session */
  messages: Message[];
  /** Pagination token for retrieving the next page of results */
  next_token: string | null;
}

/**
 * Query parameters for getting messages for a session.
 */
export interface GetMessagesParams {
  /** Maximum number of messages to return (optional, no limit if not specified, max: 1000) */
  limit?: number;
  /** Pagination token for retrieving the next page of results */
  next_token?: string | null;
}

@Injectable({
  providedIn: 'root'
})
export class SessionService {
  private http = inject(HttpClient);
  private authService = inject(AuthService);

  /**
   * Signal representing the current active session.
   * Initialized with default values to indicate no session is currently selected.
   */
  currentSession: WritableSignal<SessionMetadata> = signal<SessionMetadata>({
    sessionId: '',
    userId: '',
    title: '',
    status: 'active',
    createdAt: '',
    lastMessageAt: '',
    messageCount: 0
  });

  /**
   * Signal for pagination parameters used by the sessions resource.
   * Update this signal to trigger a refetch with new parameters.
   * Angular's resource API automatically tracks signals read within the loader,
   * so reading this signal inside the loader makes it reactive.
   */
  private sessionsParams = signal<ListSessionsParams>({});

  /**
   * Signal to hold the local sessions cache.
   * This allows us to optimistically update the UI without refetching from the API.
   */
  private localSessionsCache = signal<SessionMetadata[]>([]);

  /**
   * Signal for the session ID used by the session metadata resource.
   * Update this signal to trigger a refetch with new session ID.
   * Set to null to disable the resource.
   */
  private sessionMetadataId = signal<string | null>(null);

  /**
   * Reactive resource for fetching sessions.
   * 
   * This resource automatically refetches when `sessionsParams` signal changes
   * because Angular's resource API tracks signals read within the loader function.
   * Provides reactive signals for data, loading state, and errors.
   * 
   * The resource ensures the user is authenticated before making the HTTP request.
   * If the token is expired, it will attempt to refresh it automatically.
   * 
   * Returns SessionsListResponse which includes both the sessions array and pagination token.
   * 
   * Benefits of Angular's resource API:
   * - Automatic refetch when tracked signals change
   * - Built-in request cancellation if loader is called again before completion
   * - Seamless integration with Angular's reactivity system
   * 
   * @example
   * ```typescript
   * // Access data (may be undefined initially)
   * const response = sessionService.sessionsResource.value();
   * const sessions = response?.sessions;
   * const nextToken = response?.next_token;
   * 
   * // Check loading state
   * const isLoading = sessionService.sessionsResource.isPending();
   * 
   * // Handle errors
   * const error = sessionService.sessionsResource.error();
   * 
   * // Update pagination to trigger refetch
   * sessionService.updateSessionsParams({ limit: 50 });
   * 
   * // Get next page
   * sessionService.updateSessionsParams({ limit: 50, next_token: nextToken });
   * 
   * // Manually refetch
   * sessionService.sessionsResource.refetch();
   * ```
   */
  readonly sessionsResource = resource({
    loader: async () => {
      // Read params signal to make resource reactive to pagination changes
      const params = this.sessionsParams();

      console.log('[SessionService] Resource loader called');

      // Ensure user is authenticated before making the request
      await this.authService.ensureAuthenticated();

      // Fetch sessions from API (without merging cache here)
      return this.getSessions(params);
    }
  });

  /**
   * Computed signal that merges API sessions with local cache.
   * This automatically updates whenever either the resource data or local cache changes.
   */
  readonly mergedSessionsResource = computed(() => {
    const apiResponse = this.sessionsResource.value();
    const localCache = this.localSessionsCache();

    if (!apiResponse) {
      // Resource hasn't loaded yet, return cached sessions only
      return {
        sessions: localCache,
        next_token: null
      };
    }

    // Merge local cache with API data
    const mergedSessions = this.mergeSessions(localCache, apiResponse.sessions);

    return {
      ...apiResponse,
      sessions: mergedSessions
    };
  });

  /**
   * Updates the pagination parameters for the sessions resource.
   * This will automatically trigger a refetch of the resource.
   * 
   * @param params - New pagination parameters
   */
  updateSessionsParams(params: Partial<ListSessionsParams>): void {
    this.sessionsParams.update(current => ({ ...current, ...params }));
  }

  /**
   * Resets pagination parameters to default values and triggers a refetch.
   */
  resetSessionsParams(): void {
    this.sessionsParams.set({});
  }

  /**
   * Reactive resource for fetching session metadata.
   * 
   * This resource automatically refetches when `sessionMetadataId` signal changes.
   * Set the session ID using `setSessionMetadataId()` to fetch metadata for a specific session.
   * Set to null to disable the resource.
   * 
   * The resource ensures the user is authenticated before making the HTTP request.
   * 
   * @example
   * ```typescript
   * // Fetch metadata for a session
   * sessionService.setSessionMetadataId('session-id-123');
   * 
   * // Access data
   * const metadata = sessionService.sessionMetadataResource.value();
   * 
   * // Check loading state
   * const isLoading = sessionService.sessionMetadataResource.isPending();
   * 
   * // Manually refetch
   * sessionService.sessionMetadataResource.refetch();
   * 
   * // Disable resource
   * sessionService.setSessionMetadataId(null);
   * ```
   */
  readonly sessionMetadataResource = resource({
    loader: async () => {
      // Reading this signal inside the loader makes the resource reactive to its changes
      // Angular's resource API automatically tracks signal dependencies
      const sessionId = this.sessionMetadataId();
      
      // If no session ID, return null
      if (!sessionId) {
        return null;
      }

      // Ensure user is authenticated before making the request
      await this.authService.ensureAuthenticated();

      return this.getSessionMetadata(sessionId);
    }
  });

  /**
   * Sets the session ID for the metadata resource.
   * This will automatically trigger a refetch of the resource.
   * Set to null to disable the resource.
   * 
   * @param sessionId - Session ID to fetch metadata for, or null to disable
   */
  setSessionMetadataId(sessionId: string | null): void {
    this.sessionMetadataId.set(sessionId);
  }

  /**
   * Fetches a list of sessions from the Python API with pagination support.
   * 
   * @param params - Optional query parameters for pagination
   * @returns Promise resolving to SessionsListResponse with sessions and pagination token
   * @throws Error if the API request fails
   * 
   * @example
   * ```typescript
   * // Get first page of sessions
   * const response = await sessionService.getSessions({ limit: 20 });
   * 
   * // Get next page
   * const nextPage = await sessionService.getSessions({
   *   limit: 20,
   *   next_token: response.next_token
   * });
   * ```
   */
  async getSessions(params?: ListSessionsParams): Promise<SessionsListResponse> {
    let httpParams = new HttpParams();
    
    if (params?.limit !== undefined) {
      httpParams = httpParams.set('limit', params.limit.toString());
    }
    
    if (params?.next_token) {
      httpParams = httpParams.set('next_token', params.next_token);
    }

    try {
      const response = await firstValueFrom(
        this.http.get<SessionsListResponse>(
          `${environment.appApiUrl}/sessions`,
          { params: httpParams }
        )
      );

      return response;
    } catch (error) {
      throw error;
    }
  }

  /**
   * Fetches messages for a specific session from the Python API.
   * 
   * @param sessionId - UUID of the session
   * @param params - Optional query parameters for pagination
   * @returns Promise resolving to MessagesListResponse with messages and pagination token
   * @throws Error if the API request fails
   * 
   * @example
   * ```typescript
   * // Get first page of messages
   * const response = await sessionService.getMessages(
   *   '8e70ae89-93af-4db7-ba60-f13ea201f4cd',
   *   { limit: 20 }
   * );
   * 
   * // Get next page
   * const nextPage = await sessionService.getMessages(
   *   '8e70ae89-93af-4db7-ba60-f13ea201f4cd',
   *   { limit: 20, next_token: response.next_token }
   * );
   * ```
   */
  async getMessages(sessionId: string, params?: GetMessagesParams): Promise<MessagesListResponse> {
    let httpParams = new HttpParams();
    
    if (params?.limit !== undefined) {
      httpParams = httpParams.set('limit', params.limit.toString());
    }
    
    if (params?.next_token) {
      httpParams = httpParams.set('next_token', params.next_token);
    }

    try {
      const response = await firstValueFrom(
        this.http.get<MessagesListResponse>(
          `${environment.appApiUrl}/sessions/${sessionId}/messages`,
          { params: httpParams }
        )
      );

      return response;
    } catch (error) {
      throw error;
    }
  }

  /**
   * Fetches metadata for a specific session from the Python API.
   * 
   * @param sessionId - UUID of the session
   * @returns Promise resolving to SessionMetadata object
   * @throws Error if the API request fails
   * 
   * @example
   * ```typescript
   * const metadata = await sessionService.getSessionMetadata(
   *   '8e70ae89-93af-4db7-ba60-f13ea201f4cd'
   * );
   * ```
   */
  async getSessionMetadata(sessionId: string): Promise<SessionMetadata> {
    // Ensure user is authenticated before making the request
    await this.authService.ensureAuthenticated();

    try {
      const response = await firstValueFrom(
        this.http.get<SessionMetadata>(
          `${environment.appApiUrl}/sessions/${sessionId}/metadata`
        )
      );

      return response;
    } catch (error) {
      throw error;
    }
  }

  /**
   * Updates session metadata.
   * Performs a deep merge - only updates fields that are provided.
   * 
   * @param sessionId - UUID of the session
   * @param updates - Partial metadata updates
   * @returns Promise resolving to updated SessionMetadata object
   * @throws Error if the API request fails
   * 
   * @example
   * ```typescript
   * const updated = await sessionService.updateSessionMetadata(
   *   '8e70ae89-93af-4db7-ba60-f13ea201f4cd',
   *   { title: 'New Title', starred: true }
   * );
   * ```
   */
  async updateSessionMetadata(
    sessionId: string,
    updates: UpdateSessionMetadataRequest
  ): Promise<SessionMetadata> {
    // Ensure user is authenticated before making the request
    await this.authService.ensureAuthenticated();

    try {
      const response = await firstValueFrom(
        this.http.put<SessionMetadata>(
          `${environment.appApiUrl}/sessions/${sessionId}/metadata`,
          updates
        )
      );

      // If this is the current session, update the currentSession signal
      if (this.currentSession().sessionId === sessionId) {
        this.currentSession.update(current => ({ ...current, ...response }));
      }

      return response;
    } catch (error) {
      throw error;
    }
  }

  /**
   * Updates the title of a session.
   * 
   * @param sessionId - UUID of the session
   * @param title - New title for the session
   * @returns Promise resolving to updated SessionMetadata object
   * @throws Error if the API request fails
   */
  async updateSessionTitle(sessionId: string, title: string): Promise<SessionMetadata> {
    return this.updateSessionMetadata(sessionId, { title });
  }

  /**
   * Toggles the starred status of a session.
   * 
   * @param sessionId - UUID of the session
   * @param starred - Starred status
   * @returns Promise resolving to updated SessionMetadata object
   * @throws Error if the API request fails
   */
  async toggleStarred(sessionId: string, starred: boolean): Promise<SessionMetadata> {
    return this.updateSessionMetadata(sessionId, { starred });
  }

  /**
   * Updates the tags for a session.
   * 
   * @param sessionId - UUID of the session
   * @param tags - Array of tags
   * @returns Promise resolving to updated SessionMetadata object
   * @throws Error if the API request fails
   */
  async updateSessionTags(sessionId: string, tags: string[]): Promise<SessionMetadata> {
    return this.updateSessionMetadata(sessionId, { tags });
  }

  /**
   * Updates the status of a session.
   * 
   * @param sessionId - UUID of the session
   * @param status - Session status ('active' | 'archived' | 'deleted')
   * @returns Promise resolving to updated SessionMetadata object
   * @throws Error if the API request fails
   */
  async updateSessionStatus(
    sessionId: string,
    status: 'active' | 'archived' | 'deleted'
  ): Promise<SessionMetadata> {
    return this.updateSessionMetadata(sessionId, { status });
  }

  /**
   * Updates session preferences.
   * 
   * @param sessionId - UUID of the session
   * @param preferences - Session preferences to update
   * @returns Promise resolving to updated SessionMetadata object
   * @throws Error if the API request fails
   */
  async updateSessionPreferences(
    sessionId: string,
    preferences: {
      lastModel?: string;
      lastTemperature?: number;
      enabledTools?: string[];
      selectedPromptId?: string;
      customPromptText?: string;
    }
  ): Promise<SessionMetadata> {
    return this.updateSessionMetadata(sessionId, preferences);
  }

  /**
   * Adds a new session to the local cache optimistically.
   * This allows the UI to update immediately without waiting for an API refetch.
   * The session will appear at the top of the list until the next API refresh.
   *
   * @param sessionId - The session ID
   * @param userId - The user ID
   * @param title - Optional title for the session (defaults to empty string)
   *
   * @example
   * ```typescript
   * // When creating a new session
   * sessionService.addSessionToCache('new-session-id', 'user-123');
   * ```
   */
  addSessionToCache(sessionId: string, userId: string, title: string = ''): void {
    const newSession: SessionMetadata = {
      sessionId,
      userId,
      title,
      status: 'active',
      createdAt: new Date().toISOString(),
      lastMessageAt: new Date().toISOString(),
      messageCount: 0
    };

    console.log('[SessionService] Adding session to cache:', sessionId);
    console.log('[SessionService] Cache before update:', this.localSessionsCache());

    // Add to local cache (will be merged with API data on next load)
    this.localSessionsCache.update(sessions => {
      const updated = [newSession, ...sessions];
      console.log('[SessionService] Local cache updated. Count:', updated.length);
      return updated;
    });

    console.log('[SessionService] Cache after update:', this.localSessionsCache());
  }

  /**
   * Merges local cache sessions with API sessions.
   * Local cache sessions take precedence and appear first.
   * Deduplicates by sessionId (local cache wins).
   *
   * @param localSessions - Sessions from local cache (optimistic updates)
   * @param apiSessions - Sessions from API
   * @returns Merged and deduplicated session list
   */
  private mergeSessions(localSessions: SessionMetadata[], apiSessions: SessionMetadata[]): SessionMetadata[] {
    // If no local sessions, just return API sessions
    if (localSessions.length === 0) {
      return apiSessions;
    }

    // Create a Set of local session IDs for deduplication
    const localSessionIds = new Set(localSessions.map(s => s.sessionId));

    // Filter out API sessions that are already in local cache
    const uniqueApiSessions = apiSessions.filter(s => !localSessionIds.has(s.sessionId));

    // Return local sessions first (most recent), then unique API sessions
    return [...localSessions, ...uniqueApiSessions];
  }

  /**
   * Updates the title of a session in the local cache.
   * This allows the UI to update immediately without waiting for an API refetch.
   *
   * @param sessionId - The session ID to update
   * @param title - The new title for the session
   *
   * @example
   * ```typescript
   * // Update session title in cache
   * sessionService.updateSessionTitleInCache('session-id-123', 'New Title');
   * ```
   */
  updateSessionTitleInCache(sessionId: string, title: string): void {
    this.localSessionsCache.update(sessions => {
      return sessions.map(session => 
        session.sessionId === sessionId 
          ? { ...session, title }
          : session
      );
    });
  }


  /**
   * Clears the local session cache.
   * Useful when you want to force a full refresh from the API.
   */
  clearSessionCache(): void {
    this.localSessionsCache.set([]);
  }
}

