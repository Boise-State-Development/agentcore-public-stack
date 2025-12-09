import { inject, Injectable } from '@angular/core';
import { EventSourceMessage, fetchEventSource } from '@microsoft/fetch-event-source';
import { StreamParserService } from './stream-parser.service';
import { ChatStateService } from './chat-state.service';
import { MessageMapService } from '../session/message-map.service';
import { AuthService } from '../../../auth/auth.service';
import { environment } from '../../../../environments/environment';
import { firstValueFrom } from 'rxjs';
import { HttpClient } from '@angular/common/http';
import { SessionService } from '../session/session.service';

class RetriableError extends Error {
    constructor(message?: string) {
        super(message);
        this.name = 'RetriableError';
    }
}
class FatalError extends Error {
    constructor(message?: string) {
        super(message);
        this.name = 'FatalError';
    }
}

interface GenerateTitleRequest {
    session_id: string;
    input: string;
}

interface GenerateTitleResponse {
    title: string;
    session_id: string;
}


@Injectable({
    providedIn: 'root'
})
export class ChatHttpService {
    private streamParserService = inject(StreamParserService);
    private chatStateService = inject(ChatStateService);
    private messageMapService = inject(MessageMapService);
    private authService = inject(AuthService);
    private http = inject(HttpClient);
    private sessionService = inject(SessionService);


    async sendChatRequest(requestObject: any): Promise<void> {
        const abortController = this.chatStateService.getAbortController();

        const bearerToken = await this.getBearerTokenForStreamingResponse();

        return fetchEventSource(`${environment.inferenceApiUrl}/chat/invocations`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                // 'Authorization': `Bearer ${token}`,
                'Accept': 'text/event-stream'
            },
            body: JSON.stringify(requestObject),
            signal: abortController.signal,
            async onopen(response) {

                if (response.ok && response.headers.get('content-type')?.includes('text/event-stream')) {
                    return; // everything's good
                } else if (response.status === 403) {
                    // Handle usage limit exceeded
                    const errorData = await response.json().catch(() => ({ message: 'Forbidden' }));
                    throw new FatalError(errorData.message || 'Access forbidden');
                } else if (response.status >= 400 && response.status < 500 && response.status !== 429) {
                    // client-side errors are usually non-retriable:
                    let errorMessage = `Request failed with status ${response.status}`;
                    try {
                        const errorData = await response.json();
                        errorMessage = errorData.message || errorMessage;
                    } catch {
                        // If response is not JSON, try to get text
                        try {
                            const errorText = await response.text();
                            errorMessage = errorText || errorMessage;
                        } catch {
                            // Ignore if we can't read the response
                        }
                    }
                    throw new FatalError(errorMessage);
                } else {
                    // Server errors or unexpected status codes
                    const errorMessage = `Server error: ${response.status} ${response.statusText}`;
                    console.error('RetriableError:', errorMessage);
                    throw new RetriableError(errorMessage);
                }
            },
            onmessage: (msg: EventSourceMessage) => {
                // Parse the data if it's a string
                let parsedData = msg.data;
                if (typeof msg.data === 'string') {
                    try {
                        parsedData = JSON.parse(msg.data);
                    } catch (e) {
                        console.warn('Failed to parse SSE data:', msg.data);
                        parsedData = msg.data;
                    }
                }
                this.streamParserService.parseEventSourceMessage(msg.event, parsedData);
            },
            onclose: () => {
                this.messageMapService.endStreaming();
                this.chatStateService.setChatLoading(false);
                // Generate title for the new session (fire and forget - don't block on this)
                this.generateTitle(requestObject.session_id, requestObject.message)
                .then(response => {
                    // Update the session title in the local cache
                    this.sessionService.updateSessionTitleInCache(requestObject.session_id, response.title);
                })
                .catch(error => {
                    // Log error but don't block the user experience
                    console.error('Failed to generate session title:', error);
                });
            },
            onerror: (err) => {
                this.messageMapService.endStreaming();
                this.chatStateService.setChatLoading(false);
                
                // Display error message to user
                if (err instanceof FatalError) {
                    // Add the error as a system message
                    this.addErrorMessage(err.message);
                }
                throw err;
            }
        });
    }

    cancelChatRequest(): void {
    
        // First abort the client-side request
        this.chatStateService.abortCurrentRequest();
    
    
        this.chatStateService.setChatLoading(false);
    
        // Cleanup request-conversation mapping when cancelled
        this.chatStateService.resetState();
      }

    /**
     * Generates a title for a session based on the user's input.
     * 
     * @param sessionId - The session ID
     * @param userInput - The user's input message
     * @returns Promise resolving to GenerateTitleResponse with the generated title
     * @throws Error if the API request fails
     */
    async generateTitle(sessionId: string, userInput: string): Promise<GenerateTitleResponse> {
        const requestBody: GenerateTitleRequest = {
            session_id: sessionId,
            input: userInput
        };

        try {
            const response = await firstValueFrom(
                this.http.post<GenerateTitleResponse>(
                    `${environment.inferenceApiUrl}/chat/generate-title`,
                    requestBody
                )
            );
            return response;
        } catch (error) {
            console.error('Failed to generate title:', error);
            throw error;
        }
    }


    private addErrorMessage(errorMessage: string): void {
        console.error(errorMessage);
    }

    async getBearerTokenForStreamingResponse(): Promise<string> {
        // Get token from AuthService, refresh if expired
        let token = this.authService.getAccessToken();
        if (!token) {
            throw new FatalError('No authentication token available. Please login again.');
        }

        // Check if token needs refresh
        if (this.authService.isTokenExpired()) {
            try {
                await this.authService.refreshAccessToken();
                token = this.authService.getAccessToken();
                if (!token) {
                    throw new FatalError('Failed to refresh authentication token. Please login again.');
                }
            } catch (error) {
                throw new FatalError('Failed to refresh authentication token. Please login again.');
            }
        }

        return `Bearer ${token}`;
    }
}
