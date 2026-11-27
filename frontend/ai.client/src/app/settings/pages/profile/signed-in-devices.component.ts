import { Dialog } from '@angular/cdk/dialog';
import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroCommandLine, heroDevicePhoneMobile } from '@ng-icons/heroicons/outline';
import { firstValueFrom } from 'rxjs';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../../components/confirmation-dialog/confirmation-dialog.component';
import { formatShortDate } from '../../../shared/utils/iso-date';
import { SignedInDevice, SignedInDevicesService } from './signed-in-devices.service';

/**
 * Signed-in apps and terminals: the phone app and the command-line client, each with a Sign out
 * (native-app-auth.md D9). Web sessions aren't listed; they sign out from the account menu.
 */
@Component({
  selector: 'app-signed-in-devices',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink],
  providers: [provideIcons({ heroCommandLine, heroDevicePhoneMobile })],
  host: { class: 'block' },
  template: `
    <section
      class="rounded-lg border border-gray-200 bg-white dark:border-white/10 dark:bg-gray-800"
      aria-labelledby="signed-in-devices-heading"
    >
      <div class="border-b border-gray-200 px-6 py-4 dark:border-white/10">
        <h3 id="signed-in-devices-heading" class="text-sm/6 font-semibold text-gray-900 dark:text-white">
          Signed-in apps and terminals
        </h3>
        <p class="text-sm/6 text-gray-500 dark:text-gray-400">
          The phone app and the command-line client stay signed in for up to 30 days. Sign out any you no longer
          use, or one you've lost.
        </p>
      </div>

      @if (notice(); as text) {
        <p role="status" class="border-b border-gray-200 px-6 py-3 text-sm/6 text-gray-700 dark:border-white/10 dark:text-gray-300">
          {{ text }}
          @if (noticeLinksSchedules()) {
            <a routerLink="/schedules" class="font-medium text-primary-600 underline hover:text-primary-700 dark:text-primary-400">
              Schedules
            </a>
          }
        </p>
      }

      @if (!devices.devices.hasValue() && devices.devices.isLoading()) {
        <div aria-hidden="true" class="divide-y divide-gray-200 dark:divide-white/10">
          @for (row of skeletonRows; track row) {
            <div class="flex items-center gap-3 px-6 py-4 animate-pulse motion-reduce:animate-none">
              <div class="size-8 rounded-md bg-gray-200 dark:bg-white/10"></div>
              <div class="flex-1 space-y-2">
                <div class="h-4 w-40 rounded bg-gray-200 dark:bg-white/10"></div>
                <div class="h-3 w-56 rounded bg-gray-100 dark:bg-white/5"></div>
              </div>
            </div>
          }
        </div>
        <span role="status" class="sr-only">Loading signed-in apps and terminals</span>
      } @else if (devices.devices.error() && !devices.devices.hasValue()) {
        <p class="px-6 py-4 text-sm/6 text-red-700 dark:text-red-400">
          Couldn't load your signed-in apps and terminals. Try again later.
        </p>
      } @else if (devices.devices.hasValue() && devices.devices.value().length === 0) {
        <p class="px-6 py-4 text-sm/6 text-gray-500 dark:text-gray-400">No apps or terminals are signed in.</p>
      } @else if (devices.devices.hasValue()) {
        <ul role="list" class="divide-y divide-gray-200 dark:divide-white/10">
          @for (device of devices.devices.value(); track device.deviceId) {
            <li class="flex items-center gap-3 px-6 py-4">
              <div
                class="flex size-8 shrink-0 items-center justify-center rounded-md bg-gray-100 dark:bg-white/10"
                aria-hidden="true"
              >
                <ng-icon
                  [name]="device.kind === 'terminal' ? 'heroCommandLine' : 'heroDevicePhoneMobile'"
                  class="size-4 text-gray-500 dark:text-gray-400"
                />
              </div>
              <div class="min-w-0 flex-1">
                <p class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">
                  {{ title(device) }}
                  @if (device.current) {
                    <span class="ml-2 rounded-full bg-gray-100 px-2 py-0.5 text-xs/5 font-medium text-gray-700 dark:bg-white/10 dark:text-gray-300">
                      This device
                    </span>
                  }
                </p>
                <p class="text-sm/6 text-gray-500 dark:text-gray-400">{{ detail(device) }}</p>
              </div>
              <button
                type="button"
                class="shrink-0 rounded-md px-3 py-1.5 text-sm/6 font-medium text-red-700 ring-1 ring-inset ring-red-200 transition-colors hover:bg-red-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-600 disabled:opacity-50 dark:text-red-400 dark:ring-red-400/30 dark:hover:bg-red-400/10"
                [disabled]="signingOut() === device.deviceId"
                [attr.aria-label]="'Sign out ' + title(device)"
                (click)="signOut(device)"
              >
                Sign out
              </button>
            </li>
          }
        </ul>
      }
    </section>
  `,
})
export class SignedInDevicesComponent implements OnInit {
  readonly devices = inject(SignedInDevicesService);
  private readonly dialog = inject(Dialog);

  readonly skeletonRows = [0, 1];
  readonly signingOut = signal<string | null>(null);
  readonly notice = signal<string | null>(null);
  readonly noticeLinksSchedules = signal(false);

  ngOnInit(): void {
    this.devices.openList();
  }

  title(device: SignedInDevice): string {
    if (device.kind === 'terminal') return 'Command-line client';
    const model = device.deviceModel?.trim();
    return model ? `${model} app` : 'Phone app';
  }

  detail(device: SignedInDevice): string {
    const parts: string[] = [];
    if (device.appVersion) parts.push(`Version ${device.appVersion}`);
    const signedIn = formatShortDate(device.signedInAt);
    if (signedIn) parts.push(`Signed in ${signedIn}`);
    const used = formatShortDate(device.lastUsedAt);
    if (used) parts.push(`Last used ${used}`);
    return parts.join(' · ');
  }

  async signOut(device: SignedInDevice): Promise<void> {
    const name = this.title(device);
    const ref = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: `Sign out ${name}?`,
        message: `It will need to sign in again. Anything it was doing stops at its next request.`,
        confirmText: 'Sign out',
        cancelText: 'Cancel',
        destructive: true,
      } satisfies ConfirmationDialogData,
    });
    if ((await firstValueFrom(ref.closed)) !== true) return;

    this.signingOut.set(device.deviceId);
    this.notice.set(null);
    this.noticeLinksSchedules.set(false);
    try {
      const result = await this.devices.signOut(device.deviceId);
      if (result.scheduledRunsContinue) {
        this.notice.set(`${name} is signed out. Your scheduled runs keep running; you can stop them on`);
        this.noticeLinksSchedules.set(true);
      } else {
        this.notice.set(`${name} is signed out.`);
      }
    } catch {
      this.notice.set(`Couldn't sign out ${name}. Try again.`);
    } finally {
      this.signingOut.set(null);
    }
  }
}
