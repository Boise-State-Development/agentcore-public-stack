import { Component, ChangeDetectionStrategy, input, output } from '@angular/core';
import { Assistant } from '../models/assistant.model';

@Component({
  selector: 'app-assistant-card',
  templateUrl: './assistant-card.component.html',
  styleUrl: './assistant-card.component.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class AssistantCardComponent {
  assistant = input.required<Assistant>();
  editClicked = output<Assistant>();
  deleteClicked = output<Assistant>();

  onEdit(): void {
    this.editClicked.emit(this.assistant());
  }

  onDelete(): void {
    this.deleteClicked.emit(this.assistant());
  }
}
