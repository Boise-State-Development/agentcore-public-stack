import { inject, Injectable, signal, WritableSignal, ResourceRef, resource } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../../environments/environment';
import { AuthService } from '../../../auth/auth.service';

/**
 * Response model for a single conversation.
 * 
 * Represents a conversation stored in DynamoDB with all its metadata.
 * Matches the ConversationResponse model from the Python API.
 */
export interface Conversation {
  /** Unique identifier for the conversation (UUID) */
  conversation_id: string | null;
  /** ISO timestamp when the conversation was created */
  created_at?: string;
  /** ISO timestamp when the conversation was last updated */
  updated_at?: string;
  /** Optional title for the conversation */
  title: string | null;
  /** Optional metadata associated with the conversation */
  metadata: Record<string, unknown> | null;
}

/**
 * Response model for listing conversations with pagination support.
 * 
 * Matches the ConversationsListResponse model from the Python API.
 */
export interface ConversationsListResponse {
  /** List of conversations for the current user */
  conversations: Conversation[];
  /** Pagination token for retrieving the next page of results */
  next_token: string | null;
}

/**
 * Query parameters for listing conversations.
 */
export interface ListConversationsParams {
  /** Maximum number of conversations to return (default: 20, max: 100) */
  limit?: number;
  /** Pagination token for retrieving the next page of results */
  next_token?: string | null;
}

/**
 * Response model for a content block within a message.
 * 
 * Represents a single content block which can contain text, images, tool use, or tool results.
 * Matches the ContentBlockResponse model from the Python API.
 */
export interface ContentBlockResponse {
  /** Text content of the block */
  text?: string | null;
  /** Image content (if applicable) */
  image?: Record<string, unknown> | null;
  /** Tool use information (if applicable) */
  tool_use?: Record<string, unknown> | null;
  /** Tool execution result (if applicable) */
  tool_result?: Record<string, unknown> | null;
}

/**
 * Response model for a single message.
 * 
 * Represents a message stored in DynamoDB with all its content and metadata.
 * Matches the MessageResponse model from the Python API.
 */
export interface MessageResponse {
  /** Unique identifier for the message (UUID) */
  message_id: string;
  /** ID of the conversation this message belongs to (UUID) */
  conversation_id: string;
  /** Sequence number of the message within the conversation */
  sequence_number: number;
  /** Role of the message sender */
  role: 'user' | 'assistant' | 'system';
  /** List of content blocks in the message */
  content: ContentBlockResponse[];
  /** ISO timestamp when the message was created */
  created_at: string;
  /** Optional metadata associated with the message */
  metadata: Record<string, unknown> | null;
}

/**
 * Response model for listing messages with pagination support.
 * 
 * Matches the MessagesListResponse model from the Python API.
 */
export interface MessagesListResponse {
  /** List of messages in the conversation */
  messages: MessageResponse[];
  /** Pagination token for retrieving the next page of results */
  next_token: string | null;
}

/**
 * Query parameters for getting messages for a conversation.
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
export class ConversationService {
  private http = inject(HttpClient);
  private authService = inject(AuthService);

  /**
   * Signal representing the current active conversation.
   * Initialized as null to indicate no conversation is currently selected.
   */
  currentConversation: WritableSignal<Conversation> = signal<Conversation>({conversation_id: null, title: null, metadata: null} as Conversation);

  /**
   * Signal for pagination parameters used by the conversations resource.
   * Update this signal to trigger a refetch with new parameters.
   * Angular's resource API automatically tracks signals read within the loader,
   * so reading this signal inside the loader makes it reactive.
   */
  private conversationsParams = signal<ListConversationsParams>({ limit: 20 });

  /**
   * Reactive resource for fetching conversations.
   * 
   * This resource automatically refetches when `conversationsParams` signal changes
   * because Angular's resource API tracks signals read within the loader function.
   * Provides reactive signals for data, loading state, and errors.
   * 
   * The resource ensures the user is authenticated before making the HTTP request.
   * If the token is expired, it will attempt to refresh it automatically.
   * 
   * Benefits of Angular's resource API:
   * - Automatic refetch when tracked signals change
   * - Built-in request cancellation if loader is called again before completion
   * - Seamless integration with Angular's reactivity system
   * 
   * @example
   * ```typescript
   * // Access data (may be undefined initially)
   * const conversations = conversationService.conversationsResource.value();
   * 
   * // Check loading state
   * const isLoading = conversationService.conversationsResource.isPending();
   * 
   * // Handle errors
   * const error = conversationService.conversationsResource.error();
   * 
   * // Update pagination to trigger refetch
   * conversationService.updateConversationsParams({ limit: 50 });
   * 
   * // Manually refetch
   * conversationService.conversationsResource.refetch();
   * ```
   */
  readonly conversationsResource = resource({
    loader: async () => {
      // Ensure user is authenticated before making the request
      await this.authService.ensureAuthenticated();

      // Reading this signal inside the loader makes the resource reactive to its changes
      // Angular's resource API automatically tracks signal dependencies
      const params = this.conversationsParams();
      return this.getConversations(params);
    }
  });

  /**
   * Updates the pagination parameters for the conversations resource.
   * This will automatically trigger a refetch of the resource.
   * 
   * @param params - New pagination parameters
   */
  updateConversationsParams(params: Partial<ListConversationsParams>): void {
    this.conversationsParams.update(current => ({ ...current, ...params }));
  }

  /**
   * Resets pagination parameters to default values and triggers a refetch.
   */
  resetConversationsParams(): void {
    this.conversationsParams.set({ limit: 20 });
  }

  /**
   * Fetches a paginated list of conversations from the Python API.
   * 
   * @param params - Optional query parameters for pagination
   * @returns Promise resolving to ConversationsListResponse with conversations and pagination token
   * @throws Error if the API request fails
   * 
   * @example
   * ```typescript
   * // Get first page of conversations
   * const response = await conversationService.getConversations({ limit: 20 });
   * 
   * // Get next page
   * const nextPage = await conversationService.getConversations({ 
   *   limit: 20, 
   *   next_token: response.next_token 
   * });
   * ```
   */
  async getConversations(params?: ListConversationsParams): Promise<ConversationsListResponse> {
    let httpParams = new HttpParams();
    
    if (params?.limit !== undefined) {
      httpParams = httpParams.set('limit', params.limit.toString());
    }
    
    if (params?.next_token) {
      httpParams = httpParams.set('next_token', params.next_token);
    }

    try {
      const response = await firstValueFrom(
        this.http.get<ConversationsListResponse>(
          `${environment.appApiUrl}/conversations`,
          { params: httpParams }
        )
      );

      return response;
    } catch (error) {
      throw error;
    }
  }

  /**
   * Fetches messages for a specific conversation from the Python API.
   * 
   * @param conversationId - UUID of the conversation
   * @param params - Optional query parameters for pagination
   * @returns Promise resolving to MessagesListResponse with messages and pagination token
   * @throws Error if the API request fails
   * 
   * @example
   * ```typescript
   * // Get first page of messages
   * const response = await conversationService.getMessages(
   *   '8e70ae89-93af-4db7-ba60-f13ea201f4cd',
   *   { limit: 20 }
   * );
   * 
   * // Get next page
   * const nextPage = await conversationService.getMessages(
   *   '8e70ae89-93af-4db7-ba60-f13ea201f4cd',
   *   { limit: 20, next_token: response.next_token }
   * );
   * ```
   */
  async getMessages(conversationId: string, params?: GetMessagesParams): Promise<MessagesListResponse> {
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
          `${environment.appApiUrl}/conversations/${conversationId}/messages`,
          { params: httpParams }
        )
      );

      return response;
    } catch (error) {
      throw error;
    }
  }
}

