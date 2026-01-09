import { Component, ChangeDetectionStrategy, inject, OnInit, signal, computed } from '@angular/core';
import { Router } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { AssistantService } from './services/assistant.service';
import { AssistantListComponent } from './components/assistant-list.component';
import { Assistant } from './models/assistant.model';
import { UserService } from '../auth/user.service';

@Component({
  selector: 'app-assistants',
  templateUrl: './assistants.page.html',
  styleUrl: './assistants.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssistantListComponent, FormsModule],
})
export class AssistantsPage implements OnInit {
  private router = inject(Router);
  private assistantService = inject(AssistantService);
  private userService = inject(UserService);

  // Use service signals for reactive data
  readonly assistants = this.assistantService.assistants$;
  readonly loading = this.assistantService.loading$;
  readonly error = this.assistantService.error$;

  // Search query signal (stub - not implemented yet)
  searchQuery = signal<string>('');

  // Computed signals for filtered assistants
  readonly myAssistants = computed(() => {
    const allAssistants = this.assistants();
    const currentUser = this.userService.currentUser();
    
    if (!currentUser) {
      return [];
    }

    return allAssistants.filter(
      assistant => assistant.ownerId === currentUser.empl_id
    );
  });

  readonly publicAssistants = computed(() => {
    const allAssistants = this.assistants();
    const currentUser = this.userService.currentUser();
    
    if (!currentUser) {
      return allAssistants.filter(assistant => assistant.visibility === 'PUBLIC');
    }

    // Public assistants are those with PUBLIC visibility that are not owned by current user
    return allAssistants.filter(
      assistant => assistant.visibility === 'PUBLIC' && assistant.ownerId !== currentUser.empl_id
    );
  });

  ngOnInit(): void {
    // Load assistants from backend
    this.loadAssistants();
  }

  async loadAssistants(): Promise<void> {
    try {
      // Load COMPLETE assistants (not drafts or archived) and include public assistants
      await this.assistantService.loadAssistants(true, false, true);
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
}
