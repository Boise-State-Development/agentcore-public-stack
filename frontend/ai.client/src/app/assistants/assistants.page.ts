import { Component, ChangeDetectionStrategy, inject, OnInit } from '@angular/core';
import { Router } from '@angular/router';
import { AssistantService } from './services/assistant.service';
import { AssistantListComponent } from './components/assistant-list.component';
import { Assistant } from './models/assistant.model';

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

  // Use service signals for reactive data
  readonly assistants = this.assistantService.assistants$;
  readonly loading = this.assistantService.loading$;
  readonly error = this.assistantService.error$;

  ngOnInit(): void {
    // Load assistants from backend
    this.loadAssistants();
  }

  async loadAssistants(): Promise<void> {
    try {
      // Load only COMPLETE assistants by default (not drafts or archived)
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
}
