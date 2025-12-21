import { Component, ChangeDetectionStrategy, inject, signal, computed, OnInit } from '@angular/core';
import { Router, ActivatedRoute } from '@angular/router';
import { FormBuilder, FormGroup, FormControl, Validators, ReactiveFormsModule } from '@angular/forms';
import { QuotaStateService } from '../../services/quota-state.service';
import { QuotaTierCreate, ActionOnLimit, PeriodType } from '../../models/quota.models';

interface TierFormGroup {
  tierId: FormControl<string>;
  tierName: FormControl<string>;
  description: FormControl<string>;
  monthlyCostLimit: FormControl<number>;
  dailyCostLimit: FormControl<number | null>;
  periodType: FormControl<PeriodType>;
  softLimitPercentage: FormControl<number>;
  actionOnLimit: FormControl<ActionOnLimit>;
  enabled: FormControl<boolean>;
}

@Component({
  selector: 'app-tier-detail',
  imports: [ReactiveFormsModule],
  templateUrl: './tier-detail.component.html',
  styleUrl: './tier-detail.component.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TierDetailComponent implements OnInit {
  private fb = inject(FormBuilder);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private quotaStateService = inject(QuotaStateService);

  // Form state
  readonly isEditMode = signal<boolean>(false);
  readonly tierId = signal<string | null>(null);
  readonly isSubmitting = signal<boolean>(false);
  readonly error = this.quotaStateService.error;

  // Form group
  readonly tierForm: FormGroup<TierFormGroup> = this.fb.group({
    tierId: this.fb.control('', { nonNullable: true, validators: [Validators.required, Validators.pattern(/^[a-z0-9_-]+$/)] }),
    tierName: this.fb.control('', { nonNullable: true, validators: [Validators.required] }),
    description: this.fb.control('', { nonNullable: true }),
    monthlyCostLimit: this.fb.control(0, { nonNullable: true, validators: [Validators.required, Validators.min(0)] }),
    dailyCostLimit: this.fb.control<number | null>(null, { validators: [Validators.min(0)] }),
    periodType: this.fb.control<PeriodType>('monthly', { nonNullable: true, validators: [Validators.required] }),
    softLimitPercentage: this.fb.control(80, { nonNullable: true, validators: [Validators.required, Validators.min(0), Validators.max(100)] }),
    actionOnLimit: this.fb.control<ActionOnLimit>('block', { nonNullable: true, validators: [Validators.required] }),
    enabled: this.fb.control(true, { nonNullable: true }),
  });

  readonly pageTitle = computed(() => this.isEditMode() ? 'Edit Quota Tier' : 'Create Quota Tier');

  async ngOnInit() {
    const id = this.route.snapshot.paramMap.get('tierId');
    if (id && id !== 'new') {
      this.isEditMode.set(true);
      this.tierId.set(id);
      await this.loadTierData(id);
      // Disable tier ID editing
      this.tierForm.controls.tierId.disable();
    }
  }

  /**
   * Load tier data for editing
   */
  private async loadTierData(id: string): Promise<void> {
    try {
      await this.quotaStateService.loadTiers();
      const tier = this.quotaStateService.tiers().find(t => t.tierId === id);

      if (!tier) {
        alert('Tier not found');
        this.router.navigate(['/admin/quota/tiers']);
        return;
      }

      // Populate form with tier data
      this.tierForm.patchValue({
        tierId: tier.tierId,
        tierName: tier.tierName,
        description: tier.description || '',
        monthlyCostLimit: tier.monthlyCostLimit,
        dailyCostLimit: tier.dailyCostLimit || null,
        periodType: tier.periodType,
        softLimitPercentage: tier.softLimitPercentage,
        actionOnLimit: tier.actionOnLimit,
        enabled: tier.enabled,
      });
    } catch (error) {
      console.error('Error loading tier data:', error);
      alert('Failed to load tier data. Please try again.');
      this.router.navigate(['/admin/quota/tiers']);
    }
  }

  /**
   * Submit the form
   */
  async onSubmit(): Promise<void> {
    if (this.tierForm.invalid) {
      this.tierForm.markAllAsTouched();
      return;
    }

    this.isSubmitting.set(true);

    try {
      // Get form values, including disabled fields for edit mode
      const rawFormData = this.isEditMode()
        ? this.tierForm.getRawValue()
        : this.tierForm.value;

      const tierData: QuotaTierCreate = {
        tierId: rawFormData.tierId!,
        tierName: rawFormData.tierName!,
        description: rawFormData.description || undefined,
        monthlyCostLimit: rawFormData.monthlyCostLimit!,
        dailyCostLimit: rawFormData.dailyCostLimit || undefined,
        periodType: rawFormData.periodType!,
        softLimitPercentage: rawFormData.softLimitPercentage!,
        actionOnLimit: rawFormData.actionOnLimit!,
        enabled: rawFormData.enabled!,
      };

      if (this.isEditMode() && this.tierId()) {
        // Update existing tier
        await this.quotaStateService.updateTier(this.tierId()!, {
          tierName: tierData.tierName,
          description: tierData.description,
          monthlyCostLimit: tierData.monthlyCostLimit,
          dailyCostLimit: tierData.dailyCostLimit,
          periodType: tierData.periodType,
          softLimitPercentage: tierData.softLimitPercentage,
          actionOnLimit: tierData.actionOnLimit,
          enabled: tierData.enabled,
        });
      } else {
        // Create new tier
        await this.quotaStateService.createTier(tierData);
      }

      // Navigate back to tier list
      this.router.navigate(['/admin/quota/tiers']);
    } catch (error: any) {
      console.error('Error saving tier:', error);
      const errorMessage = error?.error?.detail || error?.message || 'Failed to save tier. Please try again.';
      alert(errorMessage);
    } finally {
      this.isSubmitting.set(false);
    }
  }

  /**
   * Cancel and navigate back
   */
  onCancel(): void {
    this.router.navigate(['/admin/quota/tiers']);
  }
}
