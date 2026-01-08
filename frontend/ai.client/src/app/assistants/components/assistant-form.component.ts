import { Component, ChangeDetectionStrategy, input, output, signal } from '@angular/core';
import { ReactiveFormsModule, FormBuilder, FormGroup, Validators } from '@angular/forms';
import { CreateAssistantRequest, UpdateAssistantRequest } from '../models/assistant.model';

@Component({
  selector: 'app-assistant-form',
  templateUrl: './assistant-form.component.html',
  styleUrl: './assistant-form.component.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReactiveFormsModule],
})
export class AssistantFormComponent {
  mode = input<'create' | 'edit'>('create');
  formSubmitted = output<CreateAssistantRequest | UpdateAssistantRequest>();
  formCancelled = output<void>();

  // TODO: Initialize form with FormBuilder
  // private fb = inject(FormBuilder);
  // form: FormGroup;

  onSubmit(): void {
    // TODO: Implement form submission
  }

  onCancel(): void {
    this.formCancelled.emit();
  }
}
