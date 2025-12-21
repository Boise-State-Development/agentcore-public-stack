import { Component, ChangeDetectionStrategy, inject, signal, computed, OnInit } from '@angular/core';
import { Router, ActivatedRoute } from '@angular/router';
import { FormBuilder, FormGroup, FormControl, Validators, ReactiveFormsModule } from '@angular/forms';
import { QuotaStateService } from '../../services/quota-state.service';
import { QuotaOverrideCreate, OverrideType } from '../../models/quota.models';

interface OverrideFormGroup {
  userId: FormControl<string>;
  overrideType: FormControl<OverrideType>;
  monthlyCostLimit: FormControl<number | null>;
  dailyCostLimit: FormControl<number | null>;
  validFrom: FormControl<string>;
  validUntil: FormControl<string>;
  reason: FormControl<string>;
  enabled: FormControl<boolean>;
}

@Component({
  selector: 'app-override-detail',
  imports: [ReactiveFormsModule],
  templateUrl: './override-detail.component.html',
  styleUrl: './override-detail.component.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class OverrideDetailComponent implements OnInit {
  private fb = inject(FormBuilder);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private quotaStateService = inject(QuotaStateService);

  // Form state
  readonly isEditMode = signal<boolean>(false);
  readonly overrideId = signal<string | null>(null);
  readonly isSubmitting = signal<boolean>(false);
  readonly error = this.quotaStateService.error;

  // Form group
  readonly overrideForm: FormGroup<OverrideFormGroup> = this.fb.group({
    userId: this.fb.control('', { nonNullable: true, validators: [Validators.required] }),
    overrideType: this.fb.control<OverrideType>('custom_limit', { nonNullable: true, validators: [Validators.required] }),
    monthlyCostLimit: this.fb.control<number | null>(null, { validators: [Validators.min(0)] }),
    dailyCostLimit: this.fb.control<number | null>(null, { validators: [Validators.min(0)] }),
    validFrom: this.fb.control('', { nonNullable: true, validators: [Validators.required] }),
    validUntil: this.fb.control('', { nonNullable: true, validators: [Validators.required] }),
    reason: this.fb.control('', { nonNullable: true, validators: [Validators.required] }),
    enabled: this.fb.control(true, { nonNullable: true }),
  });

  readonly pageTitle = computed(() => this.isEditMode() ? 'Edit Override' : 'Create Override');

  // Computed signal for showing limit fields
  readonly selectedType = computed(() => this.overrideForm.controls.overrideType.value);

  async ngOnInit() {
    const id = this.route.snapshot.paramMap.get('overrideId');
    if (id && id !== 'new') {
      this.isEditMode.set(true);
      this.overrideId.set(id);
      await this.loadOverrideData(id);
      // Disable user ID editing
      this.overrideForm.controls.userId.disable();
      this.overrideForm.controls.overrideType.disable();
    } else {
      // Set default dates (now to 30 days from now)
      const now = new Date();
      const future = new Date(now);
      future.setDate(future.getDate() + 30);

      this.overrideForm.patchValue({
        validFrom: this.formatDateForInput(now),
        validUntil: this.formatDateForInput(future),
      });
    }

    // Setup type change listener to update validators
    this.overrideForm.controls.overrideType.valueChanges.subscribe(() => {
      this.updateConditionalValidators();
    });
    this.updateConditionalValidators();
  }

  /**
   * Update conditional validators based on override type
   */
  private updateConditionalValidators(): void {
    const type = this.overrideForm.controls.overrideType.value;

    if (type === 'custom_limit') {
      // At least monthly limit required for custom limit
      this.overrideForm.controls.monthlyCostLimit.setValidators([Validators.required, Validators.min(0)]);
    } else {
      // Unlimited - no limits required
      this.overrideForm.controls.monthlyCostLimit.clearValidators();
      this.overrideForm.controls.monthlyCostLimit.setValue(null);
      this.overrideForm.controls.dailyCostLimit.setValue(null);
    }

    this.overrideForm.controls.monthlyCostLimit.updateValueAndValidity();
  }

  /**
   * Format date for datetime-local input
   */
  private formatDateForInput(date: Date): string {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    const hours = String(date.getHours()).padStart(2, '0');
    const minutes = String(date.getMinutes()).padStart(2, '0');
    return `${year}-${month}-${day}T${hours}:${minutes}`;
  }

  /**
   * Convert datetime-local input to ISO string
   */
  private dateInputToISO(dateInput: string): string {
    return new Date(dateInput).toISOString();
  }

  /**
   * Load override data for editing
   */
  private async loadOverrideData(id: string): Promise<void> {
    try {
      await this.quotaStateService.loadOverrides();
      const override = this.quotaStateService.overrides().find(o => o.overrideId === id);

      if (!override) {
        alert('Override not found');
        this.router.navigate(['/admin/quota/overrides']);
        return;
      }

      // Convert ISO dates to datetime-local format
      const validFrom = this.formatDateForInput(new Date(override.validFrom));
      const validUntil = this.formatDateForInput(new Date(override.validUntil));

      // Populate form
      this.overrideForm.patchValue({
        userId: override.userId,
        overrideType: override.overrideType,
        monthlyCostLimit: override.monthlyCostLimit || null,
        dailyCostLimit: override.dailyCostLimit || null,
        validFrom,
        validUntil,
        reason: override.reason,
        enabled: override.enabled,
      });
    } catch (error) {
      console.error('Error loading override data:', error);
      alert('Failed to load override data. Please try again.');
      this.router.navigate(['/admin/quota/overrides']);
    }
  }

  /**
   * Submit the form
   */
  async onSubmit(): Promise<void> {
    if (this.overrideForm.invalid) {
      this.overrideForm.markAllAsTouched();
      return;
    }

    this.isSubmitting.set(true);

    try {
      // Get form values, including disabled fields for edit mode
      const rawFormData = this.isEditMode()
        ? this.overrideForm.getRawValue()
        : this.overrideForm.value;

      const overrideData: QuotaOverrideCreate = {
        userId: rawFormData.userId!,
        overrideType: rawFormData.overrideType!,
        validFrom: this.dateInputToISO(rawFormData.validFrom!),
        validUntil: this.dateInputToISO(rawFormData.validUntil!),
        reason: rawFormData.reason!,
      };

      // Add limits if custom_limit type
      if (rawFormData.overrideType === 'custom_limit') {
        if (rawFormData.monthlyCostLimit !== null) {
          overrideData.monthlyCostLimit = rawFormData.monthlyCostLimit;
        }
        if (rawFormData.dailyCostLimit !== null) {
          overrideData.dailyCostLimit = rawFormData.dailyCostLimit;
        }
      }

      if (this.isEditMode() && this.overrideId()) {
        // Update existing override
        await this.quotaStateService.updateOverride(this.overrideId()!, {
          validUntil: overrideData.validUntil,
          enabled: rawFormData.enabled!,
          reason: overrideData.reason,
        });
      } else {
        // Create new override
        await this.quotaStateService.createOverride(overrideData);
      }

      // Navigate back to override list
      this.router.navigate(['/admin/quota/overrides']);
    } catch (error: any) {
      console.error('Error saving override:', error);
      const errorMessage = error?.error?.detail || error?.message || 'Failed to save override. Please try again.';
      alert(errorMessage);
    } finally {
      this.isSubmitting.set(false);
    }
  }

  /**
   * Cancel and navigate back
   */
  onCancel(): void {
    this.router.navigate(['/admin/quota/overrides']);
  }
}
