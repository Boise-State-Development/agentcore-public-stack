import { Component, signal, ChangeDetectionStrategy, inject, OnInit, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ReactiveFormsModule, FormBuilder, FormGroup, Validators, AbstractControl, ValidationErrors } from '@angular/forms';
import { Router } from '@angular/router';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { SystemService, FirstBootError } from '../../services/system.service';

@Component({
  selector: 'app-first-boot',
  imports: [CommonModule, ReactiveFormsModule],
  styleUrl: './first-boot.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="fixed inset-0 flex items-center justify-center bg-gray-50 dark:bg-gray-900 overflow-y-auto">
      <div class="w-full max-w-md px-4 py-12">
        <!-- Logo -->
        <div class="mb-8 flex justify-center">
          <img
            src="/img/logo-light.png"
            alt="Logo"
            class="size-16 dark:hidden">
          <img
            src="/img/logo-dark.png"
            alt="Logo"
            class="hidden size-16 dark:block">
        </div>

        <div class="bg-white dark:bg-gray-800 rounded-lg shadow-sm p-8">
          <div class="flex flex-col items-center gap-6">
            <div class="flex flex-col items-center gap-2">
              <h1 class="text-2xl font-semibold text-gray-900 dark:text-gray-100">
                Welcome
              </h1>
              <p class="text-base/7 text-gray-600 dark:text-gray-400 text-center">
                Create your admin account to get started
              </p>
            </div>

            <!-- Success message -->
            @if (successMessage()) {
              <div class="w-full p-4 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-lg" role="status">
                <div class="flex items-start gap-3">
                  <svg class="size-5 text-green-600 dark:text-green-400 shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
                  </svg>
                  <p class="text-sm text-green-800 dark:text-green-300">
                    {{ successMessage() }}
                  </p>
                </div>
              </div>
            }

            <!-- Error message -->
            @if (errorMessage()) {
              <div class="w-full p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg" role="alert">
                <div class="flex items-start gap-3">
                  <svg class="size-5 text-red-600 dark:text-red-400 shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                  <p class="text-sm text-red-800 dark:text-red-300">
                    {{ errorMessage() }}
                  </p>
                </div>
              </div>
            }

            <!-- Registration form -->
            @if (!successMessage()) {
              <form [formGroup]="form" (ngSubmit)="onSubmit()" class="w-full flex flex-col gap-4">
                <!-- Username -->
                <div>
                  <label for="username" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Username</label>
                  <input
                    id="username"
                    type="text"
                    formControlName="username"
                    autocomplete="username"
                    class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                    placeholder="admin"
                  />
                  @if (form.get('username')?.touched && form.get('username')?.errors) {
                    <p class="mt-1 text-xs text-red-600 dark:text-red-400">
                      @if (form.get('username')?.errors?.['required']) {
                        Username is required
                      } @else if (form.get('username')?.errors?.['minlength']) {
                        Username must be at least 3 characters
                      }
                    </p>
                  }
                </div>

                <!-- Email -->
                <div>
                  <label for="email" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Email</label>
                  <input
                    id="email"
                    type="email"
                    formControlName="email"
                    autocomplete="email"
                    class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                    placeholder="admin&#64;example.com"
                  />
                  @if (form.get('email')?.touched && form.get('email')?.errors) {
                    <p class="mt-1 text-xs text-red-600 dark:text-red-400">
                      @if (form.get('email')?.errors?.['required']) {
                        Email is required
                      } @else if (form.get('email')?.errors?.['email']) {
                        Please enter a valid email address
                      }
                    </p>
                  }
                </div>

                <!-- Password -->
                <div>
                  <label for="password" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Password</label>
                  <input
                    id="password"
                    type="password"
                    formControlName="password"
                    autocomplete="new-password"
                    class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                    placeholder="••••••••"
                  />
                  @if (form.get('password')?.touched && form.get('password')?.errors) {
                    <p class="mt-1 text-xs text-red-600 dark:text-red-400">
                      @if (form.get('password')?.errors?.['required']) {
                        Password is required
                      } @else if (form.get('password')?.errors?.['passwordStrength']) {
                        {{ form.get('password')?.errors?.['passwordStrength'] }}
                      }
                    </p>
                  }

                  <!-- Password requirements -->
                  <ul class="mt-2 text-xs text-gray-500 dark:text-gray-400 space-y-0.5">
                    <li [class.text-green-600]="passwordMeetsLength()" [class.dark:text-green-400]="passwordMeetsLength()">• At least 8 characters</li>
                    <li [class.text-green-600]="passwordHasUppercase()" [class.dark:text-green-400]="passwordHasUppercase()">• One uppercase letter</li>
                    <li [class.text-green-600]="passwordHasLowercase()" [class.dark:text-green-400]="passwordHasLowercase()">• One lowercase letter</li>
                    <li [class.text-green-600]="passwordHasDigit()" [class.dark:text-green-400]="passwordHasDigit()">• One digit</li>
                    <li [class.text-green-600]="passwordHasSymbol()" [class.dark:text-green-400]="passwordHasSymbol()">• One special character</li>
                  </ul>
                </div>

                <!-- Confirm Password -->
                <div>
                  <label for="confirmPassword" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Confirm Password</label>
                  <input
                    id="confirmPassword"
                    type="password"
                    formControlName="confirmPassword"
                    autocomplete="new-password"
                    class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                    placeholder="••••••••"
                  />
                  @if (form.get('confirmPassword')?.touched && form.get('confirmPassword')?.errors) {
                    <p class="mt-1 text-xs text-red-600 dark:text-red-400">
                      @if (form.get('confirmPassword')?.errors?.['required']) {
                        Please confirm your password
                      } @else if (form.get('confirmPassword')?.errors?.['passwordMismatch']) {
                        Passwords do not match
                      }
                    </p>
                  }
                </div>

                <!-- Submit button -->
                <button
                  type="submit"
                  [disabled]="isSubmitting() || form.invalid"
                  class="w-full mt-2 px-4 py-3 text-white font-medium rounded-lg transition-all duration-200 flex items-center justify-center gap-3 bg-blue-600 hover:bg-blue-700 disabled:opacity-60 disabled:cursor-not-allowed"
                >
                  @if (isSubmitting()) {
                    <div class="size-5 border-2 border-white border-t-transparent rounded-full animate-spin"></div>
                    <span>Creating account...</span>
                  } @else {
                    <span>Create Admin Account</span>
                  }
                </button>
              </form>
            }
          </div>
        </div>
      </div>
    </div>
  `
})
export class FirstBootPage implements OnInit, OnDestroy {
  private readonly sidenavService = inject(SidenavService);
  private readonly systemService = inject(SystemService);
  private readonly router = inject(Router);
  private readonly fb = inject(FormBuilder);

  isSubmitting = signal(false);
  errorMessage = signal<string | null>(null);
  successMessage = signal<string | null>(null);

  form: FormGroup = this.fb.group({
    username: ['', [Validators.required, Validators.minLength(3)]],
    email: ['', [Validators.required, Validators.email]],
    password: ['', [Validators.required, this.passwordStrengthValidator]],
    confirmPassword: ['', [Validators.required]],
  }, { validators: this.passwordMatchValidator });

  ngOnInit(): void {
    this.sidenavService.hide();
    this.checkFirstBootStatus();
  }

  ngOnDestroy(): void {
    this.sidenavService.show();
  }

  private async checkFirstBootStatus(): Promise<void> {
    try {
      const completed = await this.systemService.checkStatus();
      if (completed) {
        this.router.navigate(['/auth/login']);
      }
    } catch {
      // If status check fails, stay on the page — user can still try to submit
    }
  }

  // ─── Password Requirement Helpers ──────────────────────────────────

  get passwordValue(): string {
    return this.form.get('password')?.value || '';
  }

  passwordMeetsLength(): boolean {
    return this.passwordValue.length >= 8;
  }

  passwordHasUppercase(): boolean {
    return /[A-Z]/.test(this.passwordValue);
  }

  passwordHasLowercase(): boolean {
    return /[a-z]/.test(this.passwordValue);
  }

  passwordHasDigit(): boolean {
    return /\d/.test(this.passwordValue);
  }

  passwordHasSymbol(): boolean {
    return /[^A-Za-z0-9]/.test(this.passwordValue);
  }

  // ─── Validators ────────────────────────────────────────────────────

  private passwordStrengthValidator(control: AbstractControl): ValidationErrors | null {
    const value = control.value;
    if (!value) return null;

    const errors: string[] = [];
    if (value.length < 8) errors.push('at least 8 characters');
    if (!/[A-Z]/.test(value)) errors.push('one uppercase letter');
    if (!/[a-z]/.test(value)) errors.push('one lowercase letter');
    if (!/\d/.test(value)) errors.push('one digit');
    if (!/[^A-Za-z0-9]/.test(value)) errors.push('one special character');

    return errors.length > 0
      ? { passwordStrength: `Password must contain ${errors.join(', ')}` }
      : null;
  }

  private passwordMatchValidator(group: AbstractControl): ValidationErrors | null {
    const password = group.get('password')?.value;
    const confirm = group.get('confirmPassword')?.value;

    if (confirm && password !== confirm) {
      group.get('confirmPassword')?.setErrors({ passwordMismatch: true });
      return { passwordMismatch: true };
    }
    return null;
  }

  // ─── Submit ────────────────────────────────────────────────────────

  async onSubmit(): Promise<void> {
    if (this.form.invalid || this.isSubmitting()) return;

    this.isSubmitting.set(true);
    this.errorMessage.set(null);

    const { username, email, password } = this.form.value;

    try {
      await this.systemService.firstBoot(username, email, password);
      this.successMessage.set('Admin account created successfully. Redirecting to login...');

      // Redirect to login after a short delay
      setTimeout(() => {
        this.router.navigate(['/auth/login']);
      }, 2000);
    } catch (error) {
      if (error instanceof FirstBootError) {
        if (error.statusCode === 409) {
          this.errorMessage.set('First-boot setup has already been completed. Redirecting to login...');
          setTimeout(() => this.router.navigate(['/auth/login']), 2000);
        } else if (error.statusCode === 400) {
          this.errorMessage.set(error.message);
        } else {
          this.errorMessage.set(error.message);
        }
      } else {
        this.errorMessage.set('An unexpected error occurred. Please try again.');
      }
    } finally {
      this.isSubmitting.set(false);
    }
  }
}
