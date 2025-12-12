import { Component, ChangeDetectionStrategy, inject, signal, computed, OnInit } from '@angular/core';
import { Router, ActivatedRoute } from '@angular/router';
import { FormBuilder, FormGroup, FormControl, Validators, ReactiveFormsModule } from '@angular/forms';
import { AVAILABLE_ROLES, AVAILABLE_PROVIDERS, ManagedModelFormData, ModelProvider } from './models/managed-model.model';

interface ModelFormGroup {
  modelId: FormControl<string>;
  modelName: FormControl<string>;
  provider: FormControl<ModelProvider>;
  providerName: FormControl<string>;
  inputModalities: FormControl<string[]>;
  outputModalities: FormControl<string[]>;
  responseStreamingSupported: FormControl<boolean>;
  maxInputTokens: FormControl<number>;
  maxOutputTokens: FormControl<number>;
  modelLifecycle: FormControl<string | null>;
  availableToRoles: FormControl<string[]>;
  enabled: FormControl<boolean>;
  inputPricePerMillionTokens: FormControl<number>;
  outputPricePerMillionTokens: FormControl<number>;
  isReasoningModel: FormControl<boolean>;
  knowledgeCutoffDate: FormControl<string | null>;
}

@Component({
  selector: 'app-model-form-page',
  imports: [ReactiveFormsModule],
  templateUrl: './model-form.page.html',
  styleUrl: './model-form.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ModelFormPage implements OnInit {
  private fb = inject(FormBuilder);
  private router = inject(Router);
  private route = inject(ActivatedRoute);

  // Available options for multi-select fields
  readonly availableRoles = AVAILABLE_ROLES;
  readonly availableProviders = AVAILABLE_PROVIDERS;
  readonly availableModalities = ['TEXT', 'IMAGE', 'VIDEO', 'AUDIO', 'EMBEDDING'];
  readonly availableLifecycles = ['ACTIVE', 'LEGACY'];

  // Form state
  readonly isEditMode = signal<boolean>(false);
  readonly modelId = signal<string | null>(null);
  readonly isSubmitting = signal<boolean>(false);

  // Form group
  readonly modelForm: FormGroup<ModelFormGroup> = this.fb.group({
    modelId: this.fb.control('', { nonNullable: true, validators: [Validators.required] }),
    modelName: this.fb.control('', { nonNullable: true, validators: [Validators.required] }),
    provider: this.fb.control<ModelProvider>('bedrock', { nonNullable: true, validators: [Validators.required] }),
    providerName: this.fb.control('', { nonNullable: true, validators: [Validators.required] }),
    inputModalities: this.fb.control<string[]>([], { nonNullable: true, validators: [Validators.required] }),
    outputModalities: this.fb.control<string[]>([], { nonNullable: true, validators: [Validators.required] }),
    responseStreamingSupported: this.fb.control(false, { nonNullable: true }),
    maxInputTokens: this.fb.control(0, { nonNullable: true, validators: [Validators.required, Validators.min(1)] }),
    maxOutputTokens: this.fb.control(0, { nonNullable: true, validators: [Validators.required, Validators.min(1)] }),
    modelLifecycle: this.fb.control<string | null>('ACTIVE'),
    availableToRoles: this.fb.control<string[]>([], { nonNullable: true, validators: [Validators.required] }),
    enabled: this.fb.control(true, { nonNullable: true }),
    inputPricePerMillionTokens: this.fb.control(0, { nonNullable: true, validators: [Validators.required, Validators.min(0)] }),
    outputPricePerMillionTokens: this.fb.control(0, { nonNullable: true, validators: [Validators.required, Validators.min(0)] }),
    isReasoningModel: this.fb.control(false, { nonNullable: true }),
    knowledgeCutoffDate: this.fb.control<string | null>(null),
  });

  readonly pageTitle = computed(() => this.isEditMode() ? 'Edit Model' : 'Add Model');

  ngOnInit(): void {
    // Check if we're in edit mode
    const id = this.route.snapshot.paramMap.get('id');
    if (id && id !== 'new') {
      this.isEditMode.set(true);
      this.modelId.set(id);
      this.loadModelData(id);
    }

    // Check for query params (from Bedrock models page)
    const queryParams = this.route.snapshot.queryParams;
    if (queryParams['modelId']) {
      this.prefillFromQueryParams(queryParams);
    }
  }

  /**
   * Load model data for editing (mock implementation)
   */
  private loadModelData(id: string): void {
    // In a real app, this would fetch from an API
    // For now, just mock data
    console.log('Loading model data for ID:', id);
  }

  /**
   * Prefill form from query parameters (from Bedrock models page)
   */
  private prefillFromQueryParams(params: any): void {
    if (params['modelId']) {
      this.modelForm.patchValue({
        modelId: params['modelId'] || '',
        modelName: params['modelName'] || '',
        provider: params['provider'] || 'bedrock',
        providerName: params['providerName'] || '',
        inputModalities: params['inputModalities'] ? params['inputModalities'].split(',') : [],
        outputModalities: params['outputModalities'] ? params['outputModalities'].split(',') : [],
        responseStreamingSupported: params['responseStreamingSupported'] === 'true',
        maxInputTokens: params['maxInputTokens'] ? parseInt(params['maxInputTokens'], 10) : 0,
        maxOutputTokens: params['maxOutputTokens'] ? parseInt(params['maxOutputTokens'], 10) : 0,
        modelLifecycle: params['modelLifecycle'] || 'ACTIVE',
        isReasoningModel: params['isReasoningModel'] === 'true',
        knowledgeCutoffDate: params['knowledgeCutoffDate'] || null,
      });
    }
  }

  /**
   * Toggle a value in a multi-select array
   */
  toggleArrayValue(controlName: keyof ModelFormGroup, value: string): void {
    const control = this.modelForm.get(controlName) as FormControl<string[]>;
    const currentValue = control.value || [];

    if (currentValue.includes(value)) {
      control.setValue(currentValue.filter(v => v !== value));
    } else {
      control.setValue([...currentValue, value]);
    }
  }

  /**
   * Check if a value is selected in a multi-select array
   */
  isSelected(controlName: keyof ModelFormGroup, value: string): boolean {
    const control = this.modelForm.get(controlName) as FormControl<string[]>;
    return control.value?.includes(value) ?? false;
  }

  /**
   * Submit the form
   */
  async onSubmit(): Promise<void> {
    if (this.modelForm.invalid) {
      this.modelForm.markAllAsTouched();
      return;
    }

    this.isSubmitting.set(true);

    try {
      const formData = this.modelForm.value as ManagedModelFormData;

      // In a real app, this would call an API
      console.log('Saving model:', formData);

      // Simulate API call
      await new Promise(resolve => setTimeout(resolve, 500));

      // Navigate back to manage models page
      this.router.navigate(['/admin/manage-models']);
    } catch (error) {
      console.error('Error saving model:', error);
      alert('Failed to save model. Please try again.');
    } finally {
      this.isSubmitting.set(false);
    }
  }

  /**
   * Cancel and navigate back
   */
  onCancel(): void {
    this.router.navigate(['/admin/manage-models']);
  }
}
