import { inject, Injectable, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { AuthService } from '../../../auth/auth.service';
import { Message } from '../models/message.model';

// ------------------------------------------------------------------
// Interfaces
// ------------------------------------------------------------------

export interface CreateShareRequest {
  accessLevel: 'public' | 'specific';
  allowedEmails?: string[];
}

export interface UpdateShareRequest {
  accessLevel?: 'public' | 'specific';
  allowedEmails?: string[];
}

export interface ShareResponse {
  shareId: string;
  sessionId: string;
  ownerId: string;
  accessLevel: 'public' | 'specific';
  allowedEmails?: string[];
  createdAt: string;
  shareUrl: string;
}

export interface ShareListResponse {
  shares: ShareResponse[];
}

export interface SharedConversationResponse {
  shareId: string;
  title: string;
  accessLevel: 'public' | 'specific';
  createdAt: string;
  ownerId: string;
  messages: Message[];
}

export interface ExportResponse {
  sessionId: string;
  title: string;
}

// ------------------------------------------------------------------
// Service
// ------------------------------------------------------------------

@Injectable({ providedIn: 'root' })
export class ShareService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private authService = inject(AuthService);

  private readonly conversationsUrl = computed(() => `${this.config.appApiUrl()}/conversations`);
  private readonly sharesUrl = computed(() => `${this.config.appApiUrl()}/shares`);
  private readonly sharedUrl = computed(() => `${this.config.appApiUrl()}/shared`);

  async createShare(sessionId: string, accessLevel: string, allowedEmails?: string[]): Promise<ShareResponse> {
    await this.authService.ensureAuthenticated();

    const body: CreateShareRequest = {
      accessLevel: accessLevel as CreateShareRequest['accessLevel'],
      ...(allowedEmails?.length ? { allowedEmails } : {}),
    };

    return firstValueFrom(
      this.http.post<ShareResponse>(`${this.conversationsUrl()}/${sessionId}/share`, body)
    );
  }

  async listSharesForSession(sessionId: string): Promise<ShareListResponse> {
    await this.authService.ensureAuthenticated();

    return firstValueFrom(
      this.http.get<ShareListResponse>(`${this.conversationsUrl()}/${sessionId}/shares`)
    );
  }

  async getSharedConversation(shareId: string): Promise<SharedConversationResponse> {
    await this.authService.ensureAuthenticated();

    return firstValueFrom(
      this.http.get<SharedConversationResponse>(`${this.sharedUrl()}/${shareId}`)
    );
  }

  async updateShare(shareId: string, accessLevel?: string, allowedEmails?: string[]): Promise<ShareResponse> {
    await this.authService.ensureAuthenticated();

    const body: UpdateShareRequest = {};
    if (accessLevel) body.accessLevel = accessLevel as UpdateShareRequest['accessLevel'];
    if (allowedEmails) body.allowedEmails = allowedEmails;

    return firstValueFrom(
      this.http.patch<ShareResponse>(`${this.sharesUrl()}/${shareId}`, body)
    );
  }

  async revokeShare(shareId: string): Promise<void> {
    await this.authService.ensureAuthenticated();

    await firstValueFrom(
      this.http.delete(`${this.sharesUrl()}/${shareId}`)
    );
  }

  async exportSharedConversation(shareId: string): Promise<ExportResponse> {
    await this.authService.ensureAuthenticated();

    return firstValueFrom(
      this.http.post<ExportResponse>(`${this.sharesUrl()}/${shareId}/export`, {})
    );
  }
}
