import { inject, Injectable } from '@angular/core';
import { EventSourceMessage, fetchEventSource } from '@microsoft/fetch-event-source';
// import { StreamParserService } from './stream-parser.service';
import { ChatStateService } from './chat-state.service';
import { environment } from '../../../../environments/environment';
// import { MessageMapService } from '../conversation/message-map.service';
// import { AuthService } from '../../../auth/auth.service';

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

@Injectable({
    providedIn: 'root'
})
export class ChatHttpService {
    // private streamParserService = inject(StreamParserService);
    private chatStateService = inject(ChatStateService);
    // private messageMapService = inject(MessageMapService);
    // private authService = inject(AuthService);

    async sendChatRequest(requestObject: any): Promise<void> {
        const abortController = this.chatStateService.getAbortController();

        // Get token from AuthService, refresh if expired
        // let token = this.authService.getAccessToken();
        // if (!token) {
        //     throw new FatalError('No authentication token available. Please login again.');
        // }

        // Check if token needs refresh
        // if (this.authService.isTokenExpired()) {
        //     try {
        //         await this.authService.refreshAccessToken();
        //         token = this.authService.getAccessToken();
        //         if (!token) {
        //             throw new FatalError('Failed to refresh authentication token. Please login again.');
        //         }
        //     } catch (error) {
        //         throw new FatalError('Failed to refresh authentication token. Please login again.');
        //     }
        // }


        return fetchEventSource(`${environment.apiUrl}/chat/stream`, {
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
                console.log('parsedData', parsedData);
                // this.streamParserService.parseEventSourceMessage(msg.event, parsedData);
            },
            onclose: () => {
                // this.messageMapService.endStreaming();
                this.chatStateService.setChatLoading(false);
            },
            onerror: (err) => {
                // this.messageMapService.endStreaming();
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




    private addErrorMessage(errorMessage: string): void {
        console.error(errorMessage);
    }
}
