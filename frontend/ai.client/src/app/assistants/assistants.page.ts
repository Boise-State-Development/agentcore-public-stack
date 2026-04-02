import { Component, ChangeDetectionStrategy, inject, OnInit, computed } from '@angular/core';
import { Router } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { AssistantService } from './services/assistant.service';
import { AssistantListComponent } from './components/assistant-list.component';
import { Assistant } from './models/assistant.model';
import { UserService } from '../auth/user.service';
import { ConfirmationDialogComponent, ConfirmationDialogData } from '../components/confirmation-dialog/confirmation-dialog.component';
import { ShareAssistantDialogComponent, ShareAssistantDialogData } from './components/share-assistant-dialog.component';

@Component({
  selector: 'app-assistants',
  templateUrl: './assistants.page.html',
  styleUrl: './assistants.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssistantListComponent],
})
export class AssistantsPage implements OnInit {
  private router = inject(Router);
  private assistantService = inject(AssistantService);
  private userService = inject(UserService);
  private dialog = inject(Dialog);

  // Use service signals for reactive data
  readonly assistants = this.assistantService.assistants$;
  readonly loading = this.assistantService.loading$;
  readonly error = this.assistantService.error$;

  // Computed signals for filtered assistants
  readonly myAssistants = computed(() => {
    const allAssistants = this.assistants();
    const currentUser = this.userService.currentUser();
    
    if (!currentUser) {
      return [];
    }
    return allAssistants;
  });

  ngOnInit(): void {
    // Load assistants from backend
    this.loadAssistants();
  }

  async loadAssistants(): Promise<void> {
    try {
      // Load COMPLETE assistants (not drafts) and do NOT include public assistants
      await this.assistantService.loadAssistants(true, false);
    } catch (error) {
      console.error('Error loading assistants:', error);
    }
  }

  async onCreateNew(): Promise<void> {
    try {
      // Create a draft assistant with auto-generated ID
      const draft = await this.assistantService.createDraft({
        name: 'Untitled Assistant'
      });

      // Navigate to edit page with the new draft ID
      this.router.navigate(['/assistants', draft.assistantId, 'edit']);
    } catch (error) {
      console.error('Error creating draft assistant:', error);
    }
  }

  onAssistantSelected(assistant: Assistant): void {
    this.router.navigate(['/assistants', assistant.assistantId, 'edit']);
  }

  onChatRequested(assistant: Assistant): void {
    // Navigate to home with assistantId query parameter
    this.router.navigate(['/'], {
      queryParams: { assistantId: assistant.assistantId }
    });
  }

  async onShareRequested(assistant: Assistant): Promise<void> {
    const dialogRef = this.dialog.open(ShareAssistantDialogComponent, {
      data: {
        assistant
      } as ShareAssistantDialogData
    });

    const result = await firstValueFrom(dialogRef.closed);
    // TODO: Handle share result when sharing API is implemented
    console.warn('Sharing not yet implemented', result);
  }

  async onMakePublicRequested(assistant: Assistant): Promise<void> {
    try {
      await this.assistantService.updateAssistant(assistant.assistantId, {
        visibility: 'PUBLIC'
      });
    } catch (error) {
      console.error('Error making assistant public:', error);
    }
  }

  async onMakePrivateRequested(assistant: Assistant): Promise<void> {
    try {
      await this.assistantService.updateAssistant(assistant.assistantId, {
        visibility: 'PRIVATE'
      });
    } catch (error) {
      console.error('Error making assistant private:', error);
    }
  }

  async onDeleteRequested(assistant: Assistant): Promise<void> {
    const dialogRef = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: 'Delete Assistant',
        message: `Are you sure you want to delete "${assistant.name}"? This action cannot be undone.`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        destructive: true
      } as ConfirmationDialogData
    });

    const confirmed = await firstValueFrom(dialogRef.closed);
    if (confirmed) {
      try {
        await this.assistantService.deleteAssistant(assistant.assistantId);
      } catch (error) {
        console.error('Error deleting assistant:', error);
      }
    }
  }
}
