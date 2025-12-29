import { Component, ChangeDetectionStrategy, inject, signal, computed, OnInit } from '@angular/core';
import { Router, ActivatedRoute } from '@angular/router';
import { FormBuilder, FormGroup, FormControl, Validators, ReactiveFormsModule } from '@angular/forms';
import { CurrencyPipe } from '@angular/common';
import { QuotaStateService } from '../../services/quota-state.service';
import { QuotaAssignmentCreate, QuotaAssignmentType } from '../../models/quota.models';

interface AssignmentFormGroup {
  tierId: FormControl<string>;
  assignmentType: FormControl<QuotaAssignmentType>;
  userId: FormControl<string>;
  jwtRole: FormControl<string>;
  emailDomain: FormControl<string>;
  priority: FormControl<number>;
  enabled: FormControl<boolean>;
}

@Component({
  selector: 'app-assignment-detail',
  imports: [ReactiveFormsModule, CurrencyPipe],
  templateUrl: './assignment-detail.component.html',
  styleUrl: './assignment-detail.component.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class AssignmentDetailComponent implements OnInit {
  private fb = inject(FormBuilder);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private quotaStateService = inject(QuotaStateService);

  // Form state
  readonly isEditMode = signal<boolean>(false);
  readonly assignmentId = signal<string | null>(null);
  readonly isSubmitting = signal<boolean>(false);
  readonly error = this.quotaStateService.error;

  // Available tiers
  readonly tiers = this.quotaStateService.tiers;

  // Assignment types
  readonly assignmentTypes = [
    { value: QuotaAssignmentType.DIRECT_USER, label: 'Direct User', description: 'Assign to a specific user by ID' },
    { value: QuotaAssignmentType.JWT_ROLE, label: 'JWT Role', description: 'Assign to all users with a specific role' },
    { value: QuotaAssignmentType.EMAIL_DOMAIN, label: 'Email Domain', description: 'Assign to users by email domain pattern' },
    { value: QuotaAssignmentType.DEFAULT_TIER, label: 'Default Tier', description: 'Set as the default tier for all users' },
  ];

  // Form group
  readonly assignmentForm: FormGroup<AssignmentFormGroup> = this.fb.group({
    tierId: this.fb.control('', { nonNullable: true, validators: [Validators.required] }),
    assignmentType: this.fb.control<QuotaAssignmentType>(QuotaAssignmentType.DIRECT_USER, { nonNullable: true, validators: [Validators.required] }),
    userId: this.fb.control('', { nonNullable: true }),
    jwtRole: this.fb.control('', { nonNullable: true }),
    emailDomain: this.fb.control('', { nonNullable: true }),
    priority: this.fb.control(100, { nonNullable: true, validators: [Validators.required, Validators.min(0)] }),
    enabled: this.fb.control(true, { nonNullable: true }),
  });

  readonly pageTitle = computed(() => this.isEditMode() ? 'Edit Assignment' : 'Create Assignment');

  // Computed signal for showing which field
  readonly selectedType = computed(() => this.assignmentForm.controls.assignmentType.value);

  async ngOnInit() {
    await this.quotaStateService.loadTiers();

    const id = this.route.snapshot.paramMap.get('assignmentId');
    if (id && id !== 'new') {
      this.isEditMode.set(true);
      this.assignmentId.set(id);
      await this.loadAssignmentData(id);
    } else {
      // Check for query parameters to pre-populate form
      const userId = this.route.snapshot.queryParamMap.get('userId');
      const type = this.route.snapshot.queryParamMap.get('type');

      if (type) {
        this.assignmentForm.patchValue({
          assignmentType: type as QuotaAssignmentType,
        });
      }

      if (userId) {
        this.assignmentForm.patchValue({
          userId: userId,
        });
      }
    }

    // Setup type change listener to update validators
    this.assignmentForm.controls.assignmentType.valueChanges.subscribe(() => {
      this.updateConditionalValidators();
    });
    this.updateConditionalValidators();
  }

  /**
   * Update conditional validators based on assignment type
   */
  private updateConditionalValidators(): void {
    const type = this.assignmentForm.controls.assignmentType.value;

    // Reset all conditional validators
    this.assignmentForm.controls.userId.clearValidators();
    this.assignmentForm.controls.jwtRole.clearValidators();
    this.assignmentForm.controls.emailDomain.clearValidators();

    // Add required validator to the relevant field
    switch (type) {
      case QuotaAssignmentType.DIRECT_USER:
        this.assignmentForm.controls.userId.setValidators([Validators.required]);
        break;
      case QuotaAssignmentType.JWT_ROLE:
        this.assignmentForm.controls.jwtRole.setValidators([Validators.required]);
        break;
      case QuotaAssignmentType.EMAIL_DOMAIN:
        this.assignmentForm.controls.emailDomain.setValidators([Validators.required]);
        break;
    }

    // Update validity
    this.assignmentForm.controls.userId.updateValueAndValidity();
    this.assignmentForm.controls.jwtRole.updateValueAndValidity();
    this.assignmentForm.controls.emailDomain.updateValueAndValidity();
  }

  /**
   * Load assignment data for editing
   */
  private async loadAssignmentData(id: string): Promise<void> {
    try {
      await this.quotaStateService.loadAssignments();
      const assignment = this.quotaStateService.assignments().find(a => a.assignmentId === id);

      if (!assignment) {
        alert('Assignment not found');
        this.router.navigate(['/admin/quota/assignments']);
        return;
      }

      // Populate form
      this.assignmentForm.patchValue({
        tierId: assignment.tierId,
        assignmentType: assignment.assignmentType,
        userId: assignment.userId || '',
        jwtRole: assignment.jwtRole || '',
        emailDomain: assignment.emailDomain || '',
        priority: assignment.priority,
        enabled: assignment.enabled,
      });
    } catch (error) {
      console.error('Error loading assignment data:', error);
      alert('Failed to load assignment data. Please try again.');
      this.router.navigate(['/admin/quota/assignments']);
    }
  }

  /**
   * Submit the form
   */
  async onSubmit(): Promise<void> {
    if (this.assignmentForm.invalid) {
      this.assignmentForm.markAllAsTouched();
      return;
    }

    this.isSubmitting.set(true);

    try {
      const formData = this.assignmentForm.value;
      const type = formData.assignmentType!;

      const assignmentData: QuotaAssignmentCreate = {
        tierId: formData.tierId!,
        assignmentType: type,
        priority: formData.priority,
        enabled: formData.enabled,
      };

      // Add conditional fields based on type
      if (type === QuotaAssignmentType.DIRECT_USER) {
        assignmentData.userId = formData.userId!;
      } else if (type === QuotaAssignmentType.JWT_ROLE) {
        assignmentData.jwtRole = formData.jwtRole!;
      } else if (type === QuotaAssignmentType.EMAIL_DOMAIN) {
        assignmentData.emailDomain = formData.emailDomain!;
      }

      if (this.isEditMode() && this.assignmentId()) {
        // Update existing assignment
        await this.quotaStateService.updateAssignment(this.assignmentId()!, {
          tierId: assignmentData.tierId,
          priority: assignmentData.priority,
          enabled: assignmentData.enabled,
        });
      } else {
        // Create new assignment
        await this.quotaStateService.createAssignment(assignmentData);
      }

      // Navigate back to assignment list
      this.router.navigate(['/admin/quota/assignments']);
    } catch (error: any) {
      console.error('Error saving assignment:', error);
      const errorMessage = error?.error?.detail || error?.message || 'Failed to save assignment. Please try again.';
      alert(errorMessage);
    } finally {
      this.isSubmitting.set(false);
    }
  }

  /**
   * Cancel and navigate back
   */
  onCancel(): void {
    this.router.navigate(['/admin/quota/assignments']);
  }
}
