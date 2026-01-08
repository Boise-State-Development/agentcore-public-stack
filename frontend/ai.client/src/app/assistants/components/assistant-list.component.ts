import { Component, ChangeDetectionStrategy, input, output } from '@angular/core';
import { Assistant } from '../models/assistant.model';

@Component({
  selector: 'app-assistant-list',
  templateUrl: './assistant-list.component.html',
  styleUrl: './assistant-list.component.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class AssistantListComponent {
  assistants = input.required<Assistant[]>();
  assistantSelected = output<Assistant>();

  onAssistantClick(assistant: Assistant): void {
    this.assistantSelected.emit(assistant);
  }
}
